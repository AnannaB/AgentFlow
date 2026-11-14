ALTER TABLE workflow_steps
ADD COLUMN tool_version TEXT CHECK (
    tool_version IS NULL OR length(tool_version) BETWEEN 1 AND 64
);
