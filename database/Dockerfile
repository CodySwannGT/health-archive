FROM postgres:16-bookworm@sha256:efedf3595f1d6f415c08568ba171029bf54052e754cc9f030e3f2412b21f3d67 AS health_postgres
ARG AGE_COMMIT=2db2f060c4c9265a14d40f007eb8c56febf31e4c
ARG VECTOR_COMMIT=8ee86c96f0fd72390f890aa8a336fda6d3ab4c6c
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential ca-certificates curl flex bison postgresql-server-dev-16 \
    && mkdir /tmp/age /tmp/vector \
    && curl -fsSL "https://github.com/apache/age/archive/${AGE_COMMIT}.tar.gz" | tar xz --strip-components=1 -C /tmp/age \
    && curl -fsSL "https://github.com/pgvector/pgvector/archive/${VECTOR_COMMIT}.tar.gz" | tar xz --strip-components=1 -C /tmp/vector \
    && make -C /tmp/age -j2 && make -C /tmp/age install \
    && make -C /tmp/vector -j2 OPTFLAGS='' && make -C /tmp/vector install \
    && rm -rf /tmp/age /tmp/vector /var/lib/apt/lists/*

FROM health_postgres AS railway
RUN apt-get update && apt-get install -y --no-install-recommends openssl age awscli \
    && rm -rf /var/lib/apt/lists/*
COPY railway/entrypoint.sh /usr/local/bin/health-railway
COPY railway/backup.sh /usr/local/bin/health-cloud-backup
COPY railway/pg_hba.conf /etc/health-pg_hba.conf
RUN chmod 755 /usr/local/bin/health-railway /usr/local/bin/health-cloud-backup
ENTRYPOINT ["/usr/local/bin/health-railway"]
