# Google Analytics 4 tools

The server can also read and manage Google Analytics 4 (GA4) with the same
Google login as Google Ads. The GA4 tools are off by default.

## Modes

| `GOOGLE_ADS_MCP_ANALYTICS` | Tools | Extra OAuth scopes |
| :--- | :--- | :--- |
| unset / `off` | none | none |
| `read` | `analytics_reporting_*`, `analytics_insights_*`, `analytics_admin_*` | `analytics.readonly` |
| `full` | all of the above + `analytics_management_*` | `analytics.readonly`, `analytics.edit`, `analytics.manage.users` |

The scopes are added to the Google login of the server. After switching the
mode on (or from `read` to `full`), every user reconnects the MCP server once
in their client so Google asks for the new permissions.

Optional: `GOOGLE_ADS_MCP_ANALYTICS_PROPERTY_IDS=123,456` limits the tools to
those properties.

## One-time setup in Google Cloud

Use the Google Cloud project of the server's OAuth client
(`GOOGLE_ADS_MCP_OAUTH_CLIENT_ID`).

1. **APIs & Services → Library**: enable **Google Analytics Admin API** and
   **Google Analytics Data API**.
2. **Google Auth Platform → Data access** (OAuth consent screen → Scopes):
   add `.../auth/analytics.readonly`, `.../auth/analytics.edit` and
   `.../auth/analytics.manage.users`.
   - User type *Internal* (Workspace only): no Google review needed.
   - User type *External*: these are sensitive scopes. In *Testing* status
     add the users as test users; in *Production* Google shows an
     "unverified app" warning until the app is verified.
3. No developer token is needed. Users need access to the GA4 property
   itself: Viewer to read, Editor to change settings, Administrator for user
   management.

## Tools

**Reporting (Data API, read only)**
`list_properties`, `get_metadata`, `check_compatibility`, `run_report`
(any dimensions × metrics, up to 4 date ranges, simple or raw filters,
ordering, paging, totals), `run_pivot_report`, `run_realtime_report`,
`run_funnel_report` (v1alpha), `get_property_quotas`.

**Insights (read only)**
`property_overview` (totals vs previous period, channels, landing pages,
events, devices, referrals with revenue, data-quality flags),
`channel_performance` (channel × month: sessions, engaged sessions, users,
key events in total and per event, purchases, revenue, rates, revenue share),
`tracking_health` (retention, streams, enhanced measurement, key events that
recorded nothing, Google Ads links, signals, unassigned traffic, payment
providers and the site itself showing up as referral, data collected
yesterday).

**Admin (read only)**
`get_property_setup` (complete configuration in one call),
`list_resource_types`, `get_resource_schema`, `list_resources`,
`get_resource` (35 resource types, from key events to event edit rules and
settings objects), `search_change_history`, `run_access_report`.

**Management (propose & confirm)**
`manage_key_event`, `manage_custom_dimension`, `manage_custom_metric`,
`manage_annotation`, `update_property_settings`, `update_data_retention`,
`manage_google_ads_link`, `manage_data_stream`,
`update_enhanced_measurement`, `manage_user_access`,
`acknowledge_user_data_collection` and the generic `manage_resource`
(create / update / delete / archive for audiences, channel groups,
calculated metrics, event create and edit rules, Measurement Protocol
secrets, attribution, Google signals, data redaction, BigQuery / SA360 /
DV360 / Firebase / AdSense links, expanded data sets, subproperty filters).
Deleting accounts or properties is not offered.

The Admin API has no dry run. A proposal builds the request locally (unknown
fields and enum values are refused before anything is sent) and reads the
current object, so the change items show the new value next to the current
one. Only a second call with `validate_only=False` and the checksum commits.

## Notes on numbers

GA4 attributes conversions and revenue to the last non-direct click of the
session; Google Ads credits its own ad clicks (data-driven, up to 90 days).
The two never add up and should be reported side by side with their source.
Revenue is whatever the site sends in the purchase event (check whether
that includes VAT).
