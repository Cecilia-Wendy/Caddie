# Job Radar integration

Source: https://github.com/Jasmine-Liu-min/job-radar

Caddie uses Job Radar as the primary upstream opportunity radar for the autumn
recruiting library. Runtime data is fetched from:

https://raw.githubusercontent.com/Jasmine-Liu-min/job-radar/main/data/jobs.json

Quality metadata is fetched from:

https://raw.githubusercontent.com/Jasmine-Liu-min/job-radar/main/data/health_report.json

Local files in this directory are only review artifacts:

- `README.source.md`: upstream README snapshot.
- `health_report.sample.json`: upstream health report sample.
- `jobs.sample.fragment.json`: first bytes of the upstream jobs file for field
  inspection only. It may not be valid standalone JSON.

