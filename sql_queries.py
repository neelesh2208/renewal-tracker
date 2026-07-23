"""
SQL queries for renewal tracker.

NOTE:
  - %(run_date)s placeholder psycopg2 safely bind karega.
  - LIKE patterns mein % ko %% likhna zaroori hai (psycopg2 escaping).
  - Dates TEXT type mein hain -> ::date cast kiya hai.
  - Har query ka output 'event_key' column dena chahiye (unique per row).
  - district_name patient_registration mein hi hai — koi join nahi chahiye.
"""

# ============================================================
# RENEWED / NEW PLAN
# run_date ko jinka plan start hua (enrollment_date)
#
# plan_status:
#   NEW PLAN     -> pehla plan
#   RENEWAL      -> due_date se pehle renew
#   LATE RENEWAL -> due_date ke 45 din ke andar
#   REVIVAL      -> 45 din ke baad wapas aaya
# ============================================================

RENEWED_QUERY = """
WITH plan_history AS (
    SELECT
        pp.*,
        LAG(pp.enrollment_date::date) OVER (
            PARTITION BY pp.patient_id ORDER BY pp.enrollment_date::date
        ) AS prev_enrollment,
        LAG(pp.due_date::date) OVER (
            PARTITION BY pp.patient_id ORDER BY pp.enrollment_date::date
        ) AS prev_due,
        COUNT(*) OVER (
            PARTITION BY pp.patient_id ORDER BY pp.enrollment_date::date
        ) AS months_with_us
    FROM public.patient_rpp_registration pp
),

latest_roles AS (
    SELECT DISTINCT ON (pa.patient_id, pra.assigned_to_role_name)
        pa.patient_id,
        pra.assigned_to_role_name,
        pra.assigned_to_name
    FROM public.patient_rpp_assignment pra
    JOIN public.patient_appointment pa
        ON pa.patient_rpp_id = pra.patient_rpp_id
    WHERE pra.assigned_to_role_name IN ('Psychologist','Psychiatrist','Counsellor')
    ORDER BY pa.patient_id, pra.assigned_to_role_name, pra.date_created DESC
),

role_pivot AS (
    SELECT
        patient_id,
        MAX(CASE WHEN assigned_to_role_name='Psychologist' THEN assigned_to_name END) AS psychologist_name,
        MAX(CASE WHEN assigned_to_role_name='Psychiatrist' THEN assigned_to_name END) AS psychiatrist_name,
        MAX(CASE WHEN assigned_to_role_name='Counsellor'  THEN assigned_to_name END) AS counsellor_name
    FROM latest_roles
    GROUP BY patient_id
),

diagnosis_data AS (
    SELECT DISTINCT ON (patient_id)
        patient_id,
        diagnosis_name,
        primary_diagnosis
    FROM public.patient_provision_diagnosis_treatment
    ORDER BY patient_id, date_updated DESC NULLS LAST
)

SELECT
    event_key,
    patient_id,
    patient_name,
    mobile_number,
    hosp_name,
    district_name,
    plan_status,
    package_name,
    package_price,
    amount,
    enrollment_date,
    due_date,
    plan_days,
    months_with_us,
    prev_due,
    gap_days,
    psychologist_name,
    psychiatrist_name,
    counsellor_name,
    primary_diagnosis,
    patient_type,
    lead_source,
    marketing_person_name,
    gender_name,
    age
FROM (
    SELECT
        pr.patient_id || '_' || COALESCE(pp.patient_rpp_id, pp._id) AS event_key,
        pr.patient_id,
        pr.patient_name,
        pr.mobile_number::bigint       AS mobile_number,
        pp.hosp_name,
        pr.district_name,
        pr.gender_name,
        pr.age,
        pr.lead_source,
        pr.marketing_person_name,
        rp.psychologist_name,
        rp.psychiatrist_name,
        rp.counsellor_name,
        pp.enrollment_date::date       AS enrollment_date,
        pp.due_date::date              AS due_date,
        (pp.due_date::date - pp.enrollment_date::date) AS plan_days,
        pp.months_with_us::bigint      AS months_with_us,
        pp.prev_due,
        CASE WHEN pp.prev_due IS NOT NULL
             THEN (pp.enrollment_date::date - pp.prev_due)
        END                            AS gap_days,
        pp.package_name,
        pp.package_price,
        pp.amount,

        COALESCE(
            dd.primary_diagnosis,
            (SELECT string_agg(trim(both E' \n\t\r' from elem), ', ')
             FROM jsonb_array_elements_text(dd.diagnosis_name) AS elem)
        ) AS primary_diagnosis,

        CASE
            WHEN pp.prev_enrollment IS NULL THEN 'NEW PLAN'
            WHEN pp.enrollment_date::date <= pp.prev_due THEN 'RENEWAL'
            WHEN pp.enrollment_date::date <= pp.prev_due + INTERVAL '45 days'
                THEN 'LATE RENEWAL'
            ELSE 'REVIVAL'
        END AS plan_status,

        CASE
            WHEN pr.lead_source = 'Corporate' THEN 'Corporate'
            WHEN pr.lead_source = 'NTPC' THEN 'CSR'
            WHEN pr.lead_source = 'CSR' AND pp.amount = 0 THEN 'CSR'
            WHEN pr.lead_source = 'Existing Client' AND pp.amount = 0 THEN 'CSR'
            WHEN pr.csr_id IS NULL OR pr.csr_id = 'regular' THEN 'Regular'
            ELSE 'CSR'
        END AS patient_type,

        ROW_NUMBER() OVER (
            PARTITION BY pr.patient_id, pp.enrollment_date::date
            ORDER BY pp.due_date::date DESC
        ) AS rn

    FROM public.patient_registration pr
    JOIN plan_history pp        ON pr.patient_id = pp.patient_id
    LEFT JOIN role_pivot rp     ON rp.patient_id = pr.patient_id
    LEFT JOIN diagnosis_data dd ON dd.patient_id = pr.patient_id

    WHERE pp.enrollment_date::date = %(run_date)s::date
      AND LOWER(pr.patient_name) NOT LIKE 'test%%'
      AND LOWER(pr.patient_name) NOT LIKE '%%test'
) t
WHERE rn = 1
ORDER BY plan_status, enrollment_date;
"""


# ============================================================
# DROPPED
# Jinka due_date run_date tha (plan expire ho gaya)
# ============================================================

DROPPED_QUERY = """
WITH latest_plan AS (
    SELECT DISTINCT ON (prpp.patient_ref_id)
        prpp.patient_id,
        prpp.patient_ref_id,
        prpp.patient_rpp_id,
        prpp._id,
        prpp.hosp_name,
        prpp.lead_source,
        prpp.amount,
        prpp.mobile_number,
        prpp.enrollment_date::date AS enrollment_date,
        prpp.due_date::date        AS due_date,
        prpp.hold_by_name,
        prpp.hold_date,
        prpp.package_name,
        prpp.package_price
    FROM public.patient_rpp_registration prpp
    LEFT JOIN public.patient_csr_terms csr
        ON prpp._id = csr.rppobjectid
    WHERE prpp.lead_source NOT IN
          ('CSR', 'Existing Client', 'Offline-Webinar', 'NVF')
      AND csr.rppobjectid IS NULL
    ORDER BY prpp.patient_ref_id, prpp.due_date::date DESC
),

latest_roles AS (
    SELECT DISTINCT ON (pa.patient_id, pra.assigned_to_role_name)
        pa.patient_id,
        pra.assigned_to_role_name,
        pra.assigned_to_name
    FROM public.patient_rpp_assignment pra
    JOIN public.patient_appointment pa
        ON pa.patient_rpp_id = pra.patient_rpp_id
    WHERE pra.assigned_to_role_name IN ('Psychologist','Psychiatrist','Counsellor')
    ORDER BY pa.patient_id, pra.assigned_to_role_name, pra.date_created DESC
),

role_pivot AS (
    SELECT
        patient_id,
        MAX(CASE WHEN assigned_to_role_name='Psychologist' THEN assigned_to_name END) AS psychologist_name,
        MAX(CASE WHEN assigned_to_role_name='Psychiatrist' THEN assigned_to_name END) AS psychiatrist_name,
        MAX(CASE WHEN assigned_to_role_name='Counsellor'  THEN assigned_to_name END) AS counsellor_name
    FROM latest_roles
    GROUP BY patient_id
),

plan_count AS (
    SELECT patient_id, COUNT(*) AS total_plans
    FROM public.patient_rpp_registration
    GROUP BY patient_id
),

last_session AS (
    SELECT patient_id, MAX(session_date::date) AS last_session_date
    FROM public.patient_session
    WHERE session_date IS NOT NULL AND session_date <> ''
    GROUP BY patient_id
)

SELECT
    lp.patient_id || '_' || COALESCE(lp.patient_rpp_id, lp._id) AS event_key,
    lp.patient_id,
    pr.patient_name,
    lp.mobile_number::bigint                AS mobile_number,
    lp.hosp_name,
    pr.district_name,
    lp.enrollment_date,
    lp.due_date,
    (lp.due_date + 1)                       AS inactive_date,
    (CURRENT_DATE - lp.due_date)            AS days_since_expiry,
    (lp.due_date - lp.enrollment_date)      AS plan_days,
    CASE WHEN pc.total_plans = 1 THEN 'First plan drop'
         ELSE 'Repeat client drop'
    END                                     AS drop_type,
    pc.total_plans,
    lp.package_name,
    lp.package_price,
    lp.amount,
    lp.hold_by_name,
    lp.hold_date,
    rp.psychologist_name,
    rp.psychiatrist_name,
    rp.counsellor_name,
    ls.last_session_date,
    lp.lead_source,
    pr.age,
    pr.gender_name,
    'Regular'::text AS patient_type

FROM latest_plan lp
INNER JOIN public.patient_registration pr
    ON lp.patient_ref_id = pr.patient_ref_id
LEFT JOIN role_pivot rp   ON rp.patient_id = lp.patient_id
LEFT JOIN plan_count pc   ON pc.patient_id = lp.patient_id
LEFT JOIN last_session ls ON ls.patient_id = lp.patient_id

WHERE lp.due_date = %(run_date)s::date
  AND LOWER(pr.patient_name) NOT LIKE 'test%%'
  AND LOWER(pr.patient_name) NOT LIKE '%%test'

ORDER BY lp.due_date;
"""