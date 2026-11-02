-- Status `review` (2026-11-02, the user): a posting put in front of the user to read before deciding to build or pass.
-- Active (it shows on the Active tab and the markdown view) and sorted after `built`; it is not an application:
-- no folder until it moves to `built`, tracker_sync records it as a hold (trains nothing), and the reporting views
-- leave it out of application counts. Idempotent.
INSERT INTO tracker.status (status, is_active, sort_order) VALUES ('review', true, 6) ON CONFLICT DO NOTHING;
