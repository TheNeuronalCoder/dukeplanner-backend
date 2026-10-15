#!/bin/sh
set -e

# The database lives in Postgres, which isn't reachable at image build time,
# so schema migrations and the catalog fixtures are applied on each start.
# loaddata upserts by primary key, so reloading leaves user data untouched.
python manage.py migrate --noinput
python manage.py loaddata catalog/fixtures/*.json

exec "$@"
