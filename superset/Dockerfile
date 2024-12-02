FROM apache/superset:4.1.1

ARG SUPERSET_SECRET_KEY

RUN pip install kubling-sqlalchemy

ENV SUPERSET_ADMIN_USERNAME=admin
ENV SUPERSET_ADMIN_PASSWORD=admin
ENV SUPERSET_ADMIN_FIRSTNAME=Kubling
ENV SUPERSET_ADMIN_LASTNAME=Admin
ENV SUPERSET_ADMIN_EMAIL=admin@kubling.com

RUN superset db upgrade && \
    superset fab create-admin \
        --username $SUPERSET_ADMIN_USERNAME \
        --firstname $SUPERSET_ADMIN_FIRSTNAME \
        --lastname $SUPERSET_ADMIN_LASTNAME \
        --email $SUPERSET_ADMIN_EMAIL \
        --password $SUPERSET_ADMIN_PASSWORD && \
    superset init