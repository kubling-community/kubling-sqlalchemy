"""PEP 249 connection backed by one authenticated Kubling gRPC session."""

from __future__ import annotations

from threading import RLock
from typing import TYPE_CHECKING, Any

from kubling import features
from kubling.v1 import transaction_pb2, value_pb2

from kubling_sqlalchemy.transport import (
    ExecutionEnd,
    KublingGrpcClient,
    KublingLobReference,
    KublingRpcError,
    KublingSession,
    TransactionStatus,
)

from .errors import (
    Error,
    InterfaceError,
    InternalError,
    OperationalError,
    ProgrammingError,
    translate_exception,
)

if TYPE_CHECKING:
    from .cursor import Cursor


_DBAPI_OUTPUT_FEATURES = frozenset(
    {
        features.ARRAY_VALUES_V1,
        features.SPATIAL_VALUES_V1,
        features.LOB_READ_V1,
    }
)


class Connection:
    """A logical DB-API connection that exclusively owns a Kubling session."""

    def __init__(
        self,
        client: KublingGrpcClient,
        session: KublingSession,
        *,
        autocommit: bool = False,
    ) -> None:
        if type(autocommit) is not bool:
            raise InterfaceError("autocommit must be a bool")
        self._client = client
        self._session = session
        self._autocommit = autocommit
        self._closed = False
        self._lock = RLock()
        self._cursors: set[Cursor] = set()
        self._active_cursor: Cursor | None = None
        self._transaction_id: str | None = None
        self._transaction_state = transaction_pb2.TRANSACTION_STATE_NONE
        self._commit_attempted = False

    def cursor(self) -> "Cursor":
        from .cursor import Cursor

        with self._lock:
            self._ensure_open()
            cursor = Cursor(self)
            self._cursors.add(cursor)
            return cursor

    def commit(self) -> None:
        with self._lock:
            self._ensure_open()
            self._discard_active_result()
            if self._transaction_id is None:
                return
            if self._transaction_state == transaction_pb2.TRANSACTION_STATE_ROLLBACK_ONLY:
                raise OperationalError("transaction is rollback-only; call rollback()")
            if self._commit_attempted:
                raise OperationalError(
                    "the previous commit outcome is unknown; it will not be retried"
                )
            transaction_id = self._transaction_id
            self._commit_attempted = True
            try:
                status = self._session.commit_transaction(transaction_id)
            except Exception as exc:
                self._observe_error(exc, transaction_operation=True)
                raise translate_exception(exc) from exc
            self._validate_terminal_status(
                status,
                transaction_id,
                transaction_pb2.TRANSACTION_STATE_COMMITTED,
                "commit",
            )
            self._clear_transaction()

    def rollback(self) -> None:
        with self._lock:
            self._ensure_open()
            self._discard_active_result()
            if self._transaction_id is None:
                return
            transaction_id = self._transaction_id
            try:
                status = self._session.rollback_transaction(transaction_id)
            except Exception as exc:
                self._observe_error(exc, transaction_operation=True)
                raise translate_exception(exc) from exc
            self._validate_terminal_status(
                status,
                transaction_id,
                transaction_pb2.TRANSACTION_STATE_ROLLED_BACK,
                "rollback",
            )
            self._clear_transaction()

    def ping(self) -> bool:
        """Check whether the owned authenticated session is still valid."""

        with self._lock:
            self._ensure_open()
            try:
                return self._session.ping()
            except Exception as exc:
                raise translate_exception(exc) from exc

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            first_error: Error | None = None
            for cursor in tuple(self._cursors):
                cursor._close_from_connection()

            if self._transaction_id is not None and self._transaction_state in (
                transaction_pb2.TRANSACTION_STATE_ACTIVE,
                transaction_pb2.TRANSACTION_STATE_ROLLBACK_ONLY,
            ):
                try:
                    status = self._session.rollback_transaction(self._transaction_id)
                    self._validate_terminal_status(
                        status,
                        self._transaction_id,
                        transaction_pb2.TRANSACTION_STATE_ROLLED_BACK,
                        "rollback during close",
                    )
                    self._clear_transaction()
                except Exception as exc:
                    first_error = translate_exception(exc)

            try:
                self._session.close()
            except Exception as exc:
                if first_error is None:
                    first_error = translate_exception(exc)
            finally:
                self._client.close()
                self._closed = True
                self._active_cursor = None
                self._cursors.clear()
            if first_error is not None:
                raise first_error

    @property
    def autocommit(self) -> bool:
        return self._autocommit

    @autocommit.setter
    def autocommit(self, value: bool) -> None:
        if type(value) is not bool:
            raise InterfaceError("autocommit must be a bool")
        with self._lock:
            self._ensure_open()
            if value == self._autocommit:
                return
            if value:
                self.commit()
            self._autocommit = value

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def transaction_id(self) -> str | None:
        return self._transaction_id

    @property
    def transaction_state(self) -> int:
        return self._transaction_state

    @property
    def session_id(self) -> str:
        self._ensure_open()
        return self._session.session_id

    @property
    def server_version(self) -> str:
        self._ensure_open()
        return self._session.server_info.server_version

    def materialize_lob(self, reference: KublingLobReference) -> bytes | str:
        """Read a LOB reference completely and release its server resource."""

        if not isinstance(reference, KublingLobReference):
            raise ProgrammingError("materialize_lob requires a Kubling LOB reference")
        with self._lock:
            self._ensure_open()
            failure: Exception | None = None
            payload = b""
            try:
                with self._session.read_lob(reference) as stream:
                    payload = b"".join(stream)
            except Exception as exc:
                failure = exc
            try:
                self._session.release_lob(reference)
            except Exception as exc:
                if failure is None:
                    failure = exc
            if failure is not None:
                raise translate_exception(failure) from failure
        if reference.type == value_pb2.VALUE_TYPE_CLOB:
            try:
                return payload.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise InternalError("CLOB payload is not valid UTF-8") from exc
        return payload

    @property
    def _accepted_output_features(self) -> frozenset[str]:
        return _DBAPI_OUTPUT_FEATURES.intersection(self._session.advertised_features)

    def __enter__(self) -> "Connection":
        self._ensure_open()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if exc_type is None:
            self.commit()
        else:
            self.rollback()

    def _transaction_for_execute(self) -> str:
        self._ensure_open()
        if self._autocommit:
            return ""
        if self._transaction_id is None:
            try:
                self._transaction_id = self._session.begin_transaction()
            except Exception as exc:
                raise translate_exception(exc) from exc
            self._transaction_state = transaction_pb2.TRANSACTION_STATE_ACTIVE
            self._commit_attempted = False
        elif self._commit_attempted or self._transaction_state == transaction_pb2.TRANSACTION_STATE_UNKNOWN:
            raise OperationalError(
                "transaction outcome is unknown; close the connection before continuing"
            )
        elif self._transaction_state == transaction_pb2.TRANSACTION_STATE_ROLLBACK_ONLY:
            raise OperationalError("transaction is rollback-only; call rollback()")
        return self._transaction_id

    def _claim_execution(self, cursor: "Cursor") -> None:
        self._ensure_open()
        if self._active_cursor is not None and self._active_cursor is not cursor:
            raise OperationalError(
                "another cursor has an unfinished execution on this connection"
            )
        self._active_cursor = cursor

    def _release_execution(self, cursor: "Cursor") -> None:
        if self._active_cursor is cursor:
            self._active_cursor = None

    def _remove_cursor(self, cursor: "Cursor") -> None:
        self._release_execution(cursor)
        self._cursors.discard(cursor)

    def _discard_active_result(self) -> None:
        if self._active_cursor is not None:
            self._active_cursor._discard_by_connection()

    def _observe_execution_end(self, end: ExecutionEnd, expected_id: str) -> None:
        status = end.transaction
        if self._autocommit:
            if (
                status.state != transaction_pb2.TRANSACTION_STATE_NONE
                or status.transaction_id
            ):
                raise InternalError(
                    "autocommit execution returned a non-NONE transaction status"
                )
            return
        if status.transaction_id != expected_id or status.state not in (
            transaction_pb2.TRANSACTION_STATE_ACTIVE,
            transaction_pb2.TRANSACTION_STATE_ROLLBACK_ONLY,
        ):
            raise InternalError(
                "explicit execution returned an inconsistent transaction status"
            )
        self._transaction_state = status.state

    def _observe_error(
        self,
        error: Exception,
        *,
        transaction_operation: bool = False,
    ) -> None:
        if not isinstance(error, KublingRpcError) or self._transaction_id is None:
            if transaction_operation and self._transaction_id is not None:
                self._transaction_state = transaction_pb2.TRANSACTION_STATE_UNKNOWN
            return
        details = error.details
        if details.transaction_id != self._transaction_id:
            if transaction_operation or details.sql_executed is not False:
                self._transaction_state = transaction_pb2.TRANSACTION_STATE_UNKNOWN
            return
        if details.transaction_state is None:
            if transaction_operation or details.sql_executed is not False:
                self._transaction_state = transaction_pb2.TRANSACTION_STATE_UNKNOWN
            return
        self._transaction_state = details.transaction_state
        if details.transaction_state in (
            transaction_pb2.TRANSACTION_STATE_COMMITTED,
            transaction_pb2.TRANSACTION_STATE_ROLLED_BACK,
        ):
            self._clear_transaction()

    def _validate_terminal_status(
        self,
        status: TransactionStatus,
        expected_id: str,
        expected_state: int,
        operation: str,
    ) -> None:
        if status.transaction_id != expected_id or status.state != expected_state:
            self._transaction_state = status.state
            raise InternalError(f"{operation} returned an inconsistent transaction status")

    def _clear_transaction(self) -> None:
        self._transaction_id = None
        self._transaction_state = transaction_pb2.TRANSACTION_STATE_NONE
        self._commit_attempted = False

    def _ensure_open(self) -> None:
        if self._closed:
            raise InterfaceError("connection is closed")


__all__ = ["Connection"]
