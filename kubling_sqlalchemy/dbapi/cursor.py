"""Incremental PEP 249 cursor over the Kubling Execute event stream."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, NamedTuple

from kubling_sqlalchemy.transport import (
    CodecError,
    Column,
    ExecutionEnd,
    ExecutionStream,
    KublingLocalTime,
    KublingLocalTimestamp,
    ResultRows,
    ResultSetEnd,
    ResultSetStart,
    UpdateResult,
)

from .errors import (
    Error,
    InterfaceError,
    InternalError,
    NotSupportedError,
    OperationalError,
    ProgrammingError,
    translate_exception,
)


class ColumnDescription(NamedTuple):
    name: str
    type_code: int
    display_size: int | None
    internal_size: int | None
    precision: int | None
    scale: int | None
    null_ok: bool | None


class Cursor:
    """A forward-only cursor with bounded buffering of server row batches."""

    def __init__(self, connection) -> None:
        self._connection = connection
        self._closed = False
        self._stream: ExecutionStream | None = None
        self._rows: deque[tuple[Any, ...]] = deque()
        self._description: tuple[ColumnDescription, ...] | None = None
        self._columns: tuple[Column, ...] = ()
        self._rowcount = -1
        self._result_set = False
        self._result_complete = True
        self._failure: Error | None = None
        self._arraysize = 1
        self._transaction_id = ""

    @property
    def connection(self):
        return self._connection

    @property
    def description(self) -> tuple[ColumnDescription, ...] | None:
        return self._description

    @property
    def columns(self) -> tuple[Column, ...]:
        """Return lossless Kubling column metadata for higher-level adapters."""

        return self._columns

    @property
    def rowcount(self) -> int:
        return self._rowcount

    @property
    def lastrowid(self) -> None:
        return None

    @property
    def arraysize(self) -> int:
        return self._arraysize

    @arraysize.setter
    def arraysize(self, value: int) -> None:
        if type(value) is not int or value <= 0:
            raise ProgrammingError("arraysize must be a positive integer")
        self._arraysize = value

    @property
    def closed(self) -> bool:
        return self._closed

    def execute(self, operation: str, parameters: Sequence[Any] | None = None) -> "Cursor":
        with self._connection._lock:
            self._ensure_open()
            if not isinstance(operation, str) or not operation:
                raise ProgrammingError("operation must be a non-empty SQL string")
            values = self._coerce_parameters(parameters)
            self._reset_result()
            transaction_id = self._connection._transaction_for_execute()
            self._connection._claim_execution(self)
            self._transaction_id = transaction_id
            try:
                self._stream = self._connection._session.execute(
                    operation,
                    values,
                    transaction_id=transaction_id,
                    batch_size=self._arraysize,
                    accepted_features=self._connection._accepted_output_features,
                )
                self._result_complete = False
                self._read_initial_result()
            except Exception as exc:
                self._fail(exc)
            return self

    def executemany(
        self,
        operation: str,
        seq_of_parameters: Iterable[Sequence[Any]],
    ) -> "Cursor":
        self._ensure_open()
        if isinstance(seq_of_parameters, (str, bytes, bytearray, Mapping)):
            raise ProgrammingError("executemany requires an iterable of parameter sequences")
        total = 0
        executed = False
        for parameters in seq_of_parameters:
            executed = True
            self.execute(operation, parameters)
            if self.description is not None:
                self._discard_pending()
                raise NotSupportedError("executemany does not support row-producing SQL")
            if self.rowcount < 0:
                total = -1
            elif total >= 0:
                total += self.rowcount
        if not executed:
            self._reset_result()
            self._rowcount = 0
        else:
            self._rowcount = total
        return self

    def fetchone(self) -> tuple[Any, ...] | None:
        with self._connection._lock:
            self._require_result_set()
            if not self._rows and not self._result_complete:
                self._fill_rows(1)
            return self._rows.popleft() if self._rows else None

    def fetchmany(self, size: int | None = None) -> list[tuple[Any, ...]]:
        with self._connection._lock:
            self._require_result_set()
            requested = self._arraysize if size is None else size
            if type(requested) is not int or requested < 0:
                raise ProgrammingError("fetchmany size must be a non-negative integer")
            if requested == 0:
                return []
            self._fill_rows(requested)
            return [self._rows.popleft() for _ in range(min(requested, len(self._rows)))]

    def fetchall(self) -> list[tuple[Any, ...]]:
        with self._connection._lock:
            self._require_result_set()
            self._fill_rows(None)
            rows = list(self._rows)
            self._rows.clear()
            return rows

    def close(self) -> None:
        with self._connection._lock:
            if self._closed:
                return
            self._discard_pending()
            self._closed = True
            self._connection._remove_cursor(self)

    def setinputsizes(self, sizes) -> None:
        self._ensure_open()

    def setoutputsize(self, size, column=None) -> None:
        self._ensure_open()

    def __iter__(self) -> "Cursor":
        self._require_result_set()
        return self

    def __next__(self) -> tuple[Any, ...]:
        row = self.fetchone()
        if row is None:
            raise StopIteration
        return row

    def __enter__(self) -> "Cursor":
        self._ensure_open()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def _read_initial_result(self) -> None:
        assert self._stream is not None
        while True:
            event = next(self._stream)
            if isinstance(event, ResultSetStart):
                self._result_set = True
                self._columns = event.columns
                self._description = tuple(self._describe(column) for column in event.columns)
                return
            if isinstance(event, UpdateResult):
                self._rowcount = self._update_rowcount(event)
                continue
            if isinstance(event, ExecutionEnd):
                self._finish(event)
                return
            raise InternalError("execution returned an invalid initial result event")

    def _fill_rows(self, target: int | None) -> None:
        if self._failure is not None:
            raise self._failure
        while not self._result_complete and (target is None or len(self._rows) < target):
            assert self._stream is not None
            try:
                event = next(self._stream)
                if isinstance(event, ResultRows):
                    self._rows.extend(self._adapt_row(row) for row in event.rows)
                elif isinstance(event, ResultSetEnd):
                    self._rowcount = event.row_count
                elif isinstance(event, ExecutionEnd):
                    self._finish(event)
                else:
                    raise InternalError("execution returned an invalid row result event")
            except Exception as exc:
                self._fail(exc)

    def _finish(self, event: ExecutionEnd) -> None:
        self._connection._observe_execution_end(event, self._transaction_id)
        self._result_complete = True
        self._stream = None
        self._connection._release_execution(self)

    def _fail(self, error: Exception) -> None:
        if self._stream is not None:
            self._stream.close()
        self._stream = None
        self._result_complete = True
        self._connection._release_execution(self)
        self._connection._observe_error(error)
        translated = translate_exception(error)
        self._failure = translated
        if translated is error:
            raise translated
        raise translated from error

    def _discard_pending(self) -> None:
        if self._stream is not None:
            self._stream.close()
        self._stream = None
        self._rows.clear()
        self._result_complete = True
        self._connection._release_execution(self)

    def _discard_by_connection(self) -> None:
        self._discard_pending()
        self._description = None
        self._columns = ()
        self._rowcount = -1
        self._result_set = False
        self._failure = None

    def _reset_result(self) -> None:
        self._discard_pending()
        self._description = None
        self._columns = ()
        self._rowcount = -1
        self._result_set = False
        self._failure = None
        self._transaction_id = ""

    def _close_from_connection(self) -> None:
        if self._closed:
            return
        self._discard_pending()
        self._closed = True

    def _require_result_set(self) -> None:
        self._ensure_open()
        if not self._result_set:
            raise ProgrammingError("the previous operation did not produce a result set")
        if self._failure is not None:
            raise self._failure

    def _ensure_open(self) -> None:
        if self._closed:
            raise InterfaceError("cursor is closed")
        self._connection._ensure_open()

    @staticmethod
    def _coerce_parameters(parameters: Sequence[Any] | None) -> tuple[Any, ...]:
        if parameters is None:
            return ()
        if isinstance(parameters, (str, bytes, bytearray, Mapping)):
            raise ProgrammingError("qmark parameters must be a positional sequence")
        if not isinstance(parameters, Sequence):
            raise ProgrammingError("qmark parameters must be a positional sequence")
        return tuple(parameters)

    @staticmethod
    def _describe(column: Column) -> ColumnDescription:
        precision = column.precision or (
            column.declared_type.precision
            if column.declared_type.HasField("precision")
            else None
        )
        scale = column.scale or (
            column.declared_type.scale if column.declared_type.HasField("scale") else None
        )
        return ColumnDescription(
            column.name,
            column.declared_type.type,
            None,
            None,
            precision,
            scale,
            column.nullable,
        )

    @staticmethod
    def _update_rowcount(event: UpdateResult) -> int:
        if any(count is None for count in event.counts):
            return -1
        return sum(count for count in event.counts if count is not None)

    @staticmethod
    def _adapt_row(row: tuple[Any, ...]) -> tuple[Any, ...]:
        return tuple(Cursor._adapt_value(value) for value in row)

    @staticmethod
    def _adapt_value(value: Any) -> Any:
        try:
            if isinstance(value, KublingLocalTimestamp):
                return value.to_datetime()
            if isinstance(value, KublingLocalTime):
                return value.to_time()
        except CodecError:
            # Python's standard temporal types stop at microseconds. Keep the
            # lossless transport value when the server returns finer precision.
            return value
        return value


__all__ = ["ColumnDescription", "Cursor"]
