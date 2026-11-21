# Roadmap: an agent that sets up and manages Google Ads on its own

Goal: an agent can set up, build, edit, measure and clean up Google Ads
accounts end to end through this server, for every campaign type and feature
the Google Ads API allows, with every change going through propose and
confirm and the server's guardrails.

Status per phase. "Live" means merged to `main` (deployed on Vercel); "PR"
means built and tested, waiting for merge.

| Phase | Scope | Status |
| --- | --- | --- |
| 0 | API v26, write tools, propose & confirm (#4) | Live |
| 1 | Keyword Planner, conversion tracking (#7) | Live |
| 2 | Performance Max, Demand Gen, images (#9) | Live |
| 3 | Audiences, exclusions, experiments (#9) | Live |
| 4-5 | Offline conversions, bidding adjustments, extensions, Merchant Center, account overview, server instructions, hardening (#10, #11) | Live |
| 6 | Audience research (Audience Insights) (#12) | PR |
| 7 | Display, Shopping, Demand Gen video (#12) | PR |
| 8 | Account settings, portfolios, shared budgets, radius, lead forms (#12) | PR |
| 9 | Guarded removal, undo, Customer Match (#12) | PR |
| 10 | Custom segments, brand lists, account-level negative keywords | PR |
| 11 | Client accounts under a manager, user invitations, answering Merchant Center link requests | PR |
| 12 | Product groups (Shopping) and listing group filters (PMax), PMax and AI Max settings, URL exclusions | PR |
| 13 | Ad group settings, frequency caps, ad customizers, conversion adjustments, conversion value rules | PR |
| 14 | App campaigns | PR |
| 15 | Billing: payments accounts, billing setups, account budgets | PR |

## Coverage after phase 15

- **Account**: create client accounts under a manager, invite users, accept
  Merchant Center links, billing setup on an existing payments account and
  account budgets, account tracking (auto-tagging, template, suffix),
  account exclusions and account-level negative keywords.
- **Research**: keyword ideas, metrics and forecasts; audience suggestions,
  segments and composition; brand suggestions.
- **Campaign types**: Search (incl. AI Max), Performance Max (incl. retail),
  Demand Gen (image and video), Display, Shopping, App.
- **Structure**: ad groups, asset groups, keywords, negatives (campaign, ad
  group, shared, account), product groups and listing group filters.
- **Ads and assets**: RSAs, responsive display ads, Demand Gen ads, PMax
  assets, sitelinks, callouts, snippets, call, price, promotion and lead
  form assets, images and videos, ad customizers.
- **Targeting**: locations, radius, languages, schedules, devices, audiences
  (lists, interests, custom segments, Customer Match), demographics,
  placements, topics, brand lists, URL exclusions, frequency caps
  (Display).
- **Bidding and budgets**: all smart and manual strategies, portfolios,
  shared budgets, bid modifiers, data exclusions, seasonality adjustments.
- **Measurement**: conversion actions and goals, offline conversions,
  conversion adjustments (retractions, restatements), value rules,
  experiments, change history.
- **Lifecycle**: pause, enable, guarded removal, undo of recent changes,
  recommendations.

## What the API does not allow (by design out of scope)

- Creating or editing **Video campaigns** (YouTube reach, views): read-only
  in the Google Ads API. Demand Gen covers YouTube video ads.
- **Smart campaigns**, **Local Services**, **Hotel/Travel** campaigns: not
  relevant for Converted's clients; can be added later.
- **Merchant Center link requests** start in Merchant Center; the API can
  only accept or reject them.
- **Product groups deeper than the top level** (category or product type
  level 2+) need the parent levels first; the tools build one level.
- **Frequency caps on Demand Gen**: not supported by Google.
- Creating **payments accounts or profiles** (payment details), **terms of service**
  (Customer Match, lead forms), **GA4 links**: need a person in the Google
  Ads or GA4 interface.
- **Lead form leads** cannot be downloaded through the API; use the
  Google Ads webhook to a CRM.

## Not verified yet

Unit tests use real v26 types with mocked services. Every new tool still
needs one live dry run (`validate_only=True`) on a real account after
deployment.
