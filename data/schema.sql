PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    company TEXT NOT NULL DEFAULT '',
    location TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    date_found TEXT NOT NULL,
    date_posted TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    requirements_text TEXT NOT NULL DEFAULT '',
    education_requirements TEXT NOT NULL DEFAULT '',
    experience_requirements TEXT NOT NULL DEFAULT '',
    eligibility_text TEXT NOT NULL DEFAULT '',
    deadline TEXT,
    description_source TEXT,
    description_available INTEGER NOT NULL DEFAULT 0 CHECK(description_available IN (0,1)),
    enrichment_status TEXT NOT NULL DEFAULT 'not_attempted'
      CHECK(enrichment_status IN ('not_attempted','enriched','not_found','fetch_failed','not_available')),
    enriched_at TEXT,
    enrichment_notes TEXT NOT NULL DEFAULT '',
    enrichment_attempts INTEGER NOT NULL DEFAULT 0,
    enrichment_next_attempt_at TEXT,
    enrichment_terminal INTEGER NOT NULL DEFAULT 0,
    enrichment_cache_key TEXT,
    status TEXT NOT NULL DEFAULT 'new'
      CHECK(status IN ('new','evaluated','ready_for_review','applied','interview','offer','rejected','withdrawn','low_priority','normal')),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
    profile_type TEXT CHECK(profile_type IN ('Wet Lab','Bioinformatics','Drug Design','Other') OR profile_type IS NULL),
    keyword_score REAL,
    match_score REAL CHECK(match_score IS NULL OR match_score BETWEEN 0 AND 100),
    semantic_score REAL CHECK(semantic_score IS NULL OR semantic_score BETWEEN 0 AND 1),
    llm_score REAL CHECK(llm_score IS NULL OR llm_score BETWEEN 0 AND 100),
    final_score REAL,
    review_priority TEXT NOT NULL DEFAULT 'normal',
    llm_judge_status TEXT NOT NULL DEFAULT 'not_attempted',
    llm_judge_attempts INTEGER NOT NULL DEFAULT 0,
    llm_judge_next_retry_at TEXT,
    llm_judge_terminal INTEGER NOT NULL DEFAULT 0,
    llm_judge_result_json TEXT,
    source_query TEXT NOT NULL DEFAULT '',
    source_message_id TEXT NOT NULL DEFAULT '',
    source_query_status TEXT NOT NULL DEFAULT '',
    knockout_reason TEXT,
    last_evaluated TEXT,
    metrics_json TEXT,
    version_metadata TEXT,
    retry_count INTEGER DEFAULT 0,
    last_error TEXT,
    pipeline_status TEXT DEFAULT 'COMPLETED',
    is_easy_apply INTEGER DEFAULT 0,
    workplace_type TEXT,
    employment_type TEXT,
    liveness_status TEXT,
    liveness_checked_at TEXT,
    liveness_http_code INTEGER,
    liveness_detail TEXT
);

CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL UNIQUE,
    applied_at TEXT,
    cv_path TEXT,
    cover_letter_path TEXT,
    application_prep_path TEXT,
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    event_time TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_status_final_score ON jobs(status, final_score);
CREATE INDEX IF NOT EXISTS idx_jobs_active ON jobs(is_active);
CREATE INDEX IF NOT EXISTS idx_jobs_score ON jobs(match_score);
CREATE INDEX IF NOT EXISTS idx_jobs_profile ON jobs(profile_type);
CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company);
CREATE INDEX IF NOT EXISTS idx_jobs_final_score ON jobs(final_score);
CREATE INDEX IF NOT EXISTS idx_jobs_enrichment ON jobs(enrichment_status);
CREATE INDEX IF NOT EXISTS idx_events_job_id ON events(job_id, id);




CREATE TABLE IF NOT EXISTS enrichment_cache (
    cache_key TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    requirements_text TEXT NOT NULL DEFAULT '',
    education_requirements TEXT NOT NULL DEFAULT '',
    experience_requirements TEXT NOT NULL DEFAULT '',
    eligibility_text TEXT NOT NULL DEFAULT '',
    deadline TEXT,
    date_posted TEXT,
    description_source TEXT,
    source_score REAL,
    attempts INTEGER NOT NULL DEFAULT 0,
    fetched_at TEXT NOT NULL,
    next_retry_at TEXT,
    terminal INTEGER NOT NULL DEFAULT 0,
    notes TEXT
);
