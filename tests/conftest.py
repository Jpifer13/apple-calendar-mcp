import os

# Pin the timezone so date arithmetic and DST assertions are reproducible on
# any machine; datetimes.local_timezone() honours TZ.
os.environ.setdefault("TZ", "America/Los_Angeles")
