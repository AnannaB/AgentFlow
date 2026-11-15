ALTER TABLE approval_requests
ADD COLUMN purpose TEXT NOT NULL DEFAULT 'workflow'
CHECK (purpose IN ('workflow', 'report_export'));

CREATE INDEX idx_approval_requests_pending
ON approval_requests(status, requested_at, id);
