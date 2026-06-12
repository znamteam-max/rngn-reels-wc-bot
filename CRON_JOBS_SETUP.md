# cron-jobs.org setup

```text
Title:
RNGN | Бегущая Строка

URL:
Use the complete URL from ignored local .cron-jobs-url.txt

Method:
GET

Schedule:
Every 2 minutes

Headers:
empty

Request body:
empty

Timeout:
30 seconds

Expected status:
200

Notifications:
only on failure
```

The endpoint refreshes football and tennis caches in one request. A wrong or missing cron secret returns HTTP `401`.
