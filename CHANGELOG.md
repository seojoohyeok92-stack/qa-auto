# CHANGELOG

## Phase 8.3 - Dashboard DPS Runtime Fix

- Replaced the policy-gated Dashboard DPS path with an explicit shared-orchestrator lookup
- Persisted ordinary Naver order IDs and actual order dates after order lookup
- Blocked product-order IDs and missing order dates before Agent invocation
- Added inquiry-scoped DPS session state, correlation IDs, DB reload, and UI refresh
- Distinguished Agent offline, response timeout, login, Chrome, not-found, and save failures
- Added DPS runtime trace fields and Activity Log lifecycle events
- Verified real Dashboard button invocations through the Windows Agent and Chrome automation
- Verified 695 tests; Naver answer posting remains locked and Phase 9 was not started

## Phase 8.2 - Dashboard Workspace Reconstruction

- Rebuilt the Dashboard as a two-row operations workspace based on Dashboard.png
- Moved the compact review workspace directly below the inquiry list
- Added Program Answer, Staff Edit, and Final Answer segmented switching
- Replaced the vertical answer chain with a compact validator status bar
- Redesigned KPI cards with inline SVG icons and real seven-day sparklines
- Added a live-status topbar and grouped system-status sidebar
- Added 10/20/50 inquiry pagination and inquiry-ID selection
- Preserved Sync, Order, DPS, GPT, Approval, Activity Log, and Naver post lock
- Verified 684 tests and a real Windows Streamlit health/AppTest render

## Phase 8.1 - Dashboard Hotfix

- Added an explicit Naver inquiry sync action backed by a shared orchestrator
- Separated API synchronization from DB-only screen refresh
- Rebuilt the inquiry list as fixed-height, scrolling, single-line rows
- Moved full inquiry text and existing Naver answers into the detail panel
- Renamed the completion KPI to approval completion and exposed the post lock
- Connected manual order lookup to the common workflow step
- Added safe sync summaries and correlation-only UI errors
- Verified 675 tests and a real two-store Naver inquiry sync

## Phase 8 - Production Dashboard UX

- Enlarged the dark Dashboard typography and operational card hierarchy
- Added a shared Dashboard DPS lookup orchestrator with order ID safety checks
- Enabled manual-only OpenAI Responses API generation with configurable GPT-5.6 models
- Added Program Answer model, latency, token, and estimated-cost metadata
- Simplified UAT to five operator cards and retained developer diagnostics
- Preserved Staff Edit, Approval, Final Answer, Activity Log, and Naver post lock
- Verified 660 tests, real GPT generation, and real DPS Agent result persistence

## v1.1.0 - Order service foundation

- Added `services/order_service.py`
- Added process-local TTL cache in `services/cache_manager.py`
- Added 16-digit order-number extraction from structured fields and inquiry text
- Added automatic fallback: order ID -> product-order ID
- Added normalized dashboard order schema
- Added safe work-item enrichment helper
- Added basic unit tests
- No UI files changed
