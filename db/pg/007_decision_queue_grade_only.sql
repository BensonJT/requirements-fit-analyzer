-- 'grade': a queued row that carries lens grades and no decision (blind or seen grading in the feedback
-- applet). apply-queue writes its lens grades only; nothing goes to `decisions`.
ALTER TABLE jobs.decision_queue DROP CONSTRAINT IF EXISTS decision_queue_decision_check;
ALTER TABLE jobs.decision_queue ADD CONSTRAINT decision_queue_decision_check
  CHECK (decision IN ('build', 'pass', 'hold', 'grade'));
