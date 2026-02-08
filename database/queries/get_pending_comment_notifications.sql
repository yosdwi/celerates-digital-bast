WITH cte_timesheet_owner AS (
SELECT a.id as timesheet_id,
    c."Notification_Email",
    a.date as timesheet_date
FROM pc38r6u1npuq0ul.timesheet a
LEFT JOIN pc38r6u1npuq0ul."_nc_m2m_timesheet_Employee Data" b
    ON CAST(a.id as VARCHAR) = CAST(b.timesheet_id as VARCHAR)
LEFT JOIN pc38r6u1npuq0ul."Employee Data" c
    ON CAST(b."Employee Data_id" as VARCHAR) = CAST(c.id as VARCHAR)
)
SELECT b.base_id as schema_id,
    b.table_name,
    a.id as comment_id,
    a.row_id as row_id,
    a.comment,
    d.timesheet_date,
    a.created_by_email,
    d."Notification_Email",
    c.sent_status
FROM public.nc_comments a
LEFT JOIN public.nc_models_v2 b
    ON a.fk_model_id = b.id
LEFT JOIN phwyg5g01wfihje.sent_notifications c
    ON a.id = c.comment_id
LEFT JOIN cte_timesheet_owner d
    ON CAST(a.row_id as VARCHAR) = CAST(d.timesheet_id as VARCHAR)
WHERE 1=1
    AND sent_status IS NOT True
    AND b.table_name = 'timesheet'
    AND d.timesheet_id IS NOT NULL
