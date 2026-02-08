DECLARE @NRP AS VARCHAR(20) = ?;
DECLARE @RANGE_START AS DATE = ?;
DECLARE @RANGE_END AS DATE = ?;

WITH data_raw AS (
    SELECT
          h.[attendance_date],
          LEFT(CONVERT(VARCHAR(5), CAST(h.attendance_hour AS TIME), 108), 5)
             + ' (' + h.trans + ')' AS att_hour_label,
          h.[nrp],
          u.[name],
          h.dstrct_code
    FROM [db_attendance].[attend].[tbl_t_att_daily_history] h
    LEFT JOIN [db_pamamobile].[dbo].[tbl_user] u
           ON u.nrp = h.nrp
    WHERE h.nrp = @NRP
      AND h.attendance_date BETWEEN @RANGE_START AND @RANGE_END
      AND u.is_pama = 0 AND active = 1

    UNION ALL

    SELECT
          d.[attendance_date],
          LEFT(CONVERT(VARCHAR(5), CAST(d.attendance_hour AS TIME), 108), 5)
             + ' (' + d.trans + ')' AS att_hour_label,
          d.[nrp],
          u.[name],
          d.dstrct_code
    FROM [db_attendance].[attend].[tbl_t_att_daily] d
    LEFT JOIN [db_pamamobile].[dbo].[tbl_user] u
           ON u.nrp = d.nrp
    WHERE d.nrp = @NRP
      AND d.attendance_date BETWEEN @RANGE_START AND @RANGE_END
      AND u.is_pama = 0 AND active = 1
)
SELECT
    r.attendance_date,
    r.nrp,
    r.name,
    r.dstrct_code,
    STUFF((
        SELECT ', ' + att_hour_label
        FROM data_raw x
        WHERE x.attendance_date = r.attendance_date
          AND x.nrp = r.nrp
        ORDER BY att_hour_label
        FOR XML PATH(''), TYPE
    ).value('.', 'NVARCHAR(MAX)'), 1, 2, '') AS attendance_hour_group
FROM data_raw r
GROUP BY r.attendance_date, r.nrp, r.name, r.dstrct_code
ORDER BY r.attendance_date;
