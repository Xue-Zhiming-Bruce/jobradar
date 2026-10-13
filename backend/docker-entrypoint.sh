#!/bin/sh
# The image's one startup step (#47): the database is brought to the revision
# history this image carries, and only then does the command below serve.
#
# `alembic upgrade head` runs from `/app`, where `alembic.ini` and `migrations/`
# sit beside `app/`, and it reads `DATABASE_URL` through the app's own
# `app.db.database_url()` -- `migrations/env.py` is the one other caller -- so
# the upgrade and the app connect to the same database, and this script names
# no URL, no variable and no credential.
#
# On a database that already holds rows it adopts the schema that is there: the
# baseline revision creates a table only when it is missing. A second run at
# `head` sends no DDL and changes no row.
#
# `set -e` is the failure mode. An upgrade that fails exits non-zero here, so
# the `exec` below never runs and uvicorn never starts against a schema the
# database does not match.
set -e

alembic upgrade head

exec "$@"
