"""The job-finding layer: rules, scoring, tracker mirror, decisions, and the daily Jobs_Found report.

Nothing here imports scikit-learn, numpy or an embedding library at module load, so the
ATS sweep still runs when those optional dependencies are absent.
"""
