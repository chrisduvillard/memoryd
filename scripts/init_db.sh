#!/usr/bin/env bash
# Create role + database + schema. Run as a user with postgres superuser access.
set -euo pipefail
DB="${MEMORYD_DB:-memoryd}"
ROLE="${MEMORYD_ROLE:-memoryd}"
psql -v ON_ERROR_STOP=1 -d postgres -c "DO \$\$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='${ROLE}') THEN
    CREATE ROLE ${ROLE} LOGIN; END IF; END \$\$;"
psql -d postgres -tc "SELECT 1 FROM pg_database WHERE datname='${DB}'" | grep -q 1 \
  || createdb -O "${ROLE}" "${DB}"
psql -v ON_ERROR_STOP=1 -d "${DB}" -f "$(dirname "$0")/../migrations/001_init.sql"
psql -d "${DB}" -c "GRANT ALL ON ALL TABLES IN SCHEMA public TO ${ROLE};
                    GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO ${ROLE};"
echo "memoryd database ready: ${DB}"
