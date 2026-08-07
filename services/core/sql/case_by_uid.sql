-- Просмотр дела и всех привязанных к нему сущностей по УИД.
-- Открой в DBeaver (SQL Editor), выполни нужный запрос (Ctrl+Enter).
--
-- !!! УИД — это СТРОКА, всегда в ОДИНАРНЫХ КАВЫЧКАХ: '77MS0466-01-2026-003751-93'.
--     Без кавычек Postgres примет начало за число и выдаст
--     "trailing junk after numeric literal".
--
-- !!! На один УИД строк может быть НЕСКОЛЬКО: карточка — это тройка «УИД + суд + номер
--     дела». Один и тот же УИД встречается в разных судах (дело шло по инстанциям) и в
--     одном суде с разными номерами (приказное производство, затем исковое).
--
-- Чтобы посмотреть другое дело — замени значение УИД (по одному месту на запрос,
-- отмечено «<-- УИД ЗДЕСЬ»).


-- ============================================================================
-- Запрос 1. Компактно: одна строка на КАРТОЧКУ, привязки собраны в колонки
--           (судьи / стороны — каждая сущность с новой строки внутри ячейки).
-- ============================================================================
SELECT
    c.id,
    c.uid,
    c.code,
    ct.name || ' (' || ct.code || ')'                                AS court,
    c.status,
    c.category,
    c.receipt_date,
    (SELECT string_agg(j.full_name, E'\n' ORDER BY j.full_name)
       FROM case_judge cj
       JOIN judge j ON j.id = cj.judge_id
      WHERE cj.case_id = c.id)                                       AS judges,
    (SELECT string_agg(
                CASE s.type::text
                    WHEN 'PLAINTIFF' THEN 'Истец'
                    WHEN 'DEFENDANT' THEN 'Ответчик'
                    ELSE 'Другое'
                END || ': ' || s.full_name,
                E'\n' ORDER BY s.type::text)
       FROM case_side cs
       JOIN side s ON s.id = cs.side_id
      WHERE cs.case_id = c.id)                                       AS sides
FROM "case" c
JOIN court ct ON ct.id = c.court_id
WHERE c.uid = '77MS0466-01-2026-003751-93'   -- <-- УИД ЗДЕСЬ
ORDER BY c.id;


-- ============================================================================
-- Запрос 2. Детально: по одной строке на КАЖДУЮ привязанную сущность
--           (номер карточки + тип + значение). УИД задаётся один раз в CTE ниже.
-- ============================================================================
WITH c AS (
    SELECT id, code, court_id
      FROM "case"
     WHERE uid = '77MS0466-01-2026-003751-93'   -- <-- УИД ЗДЕСЬ
)
SELECT c.code AS case_code, 'Суд' AS entity, ct.name || ' (' || ct.code || ')' AS value
  FROM c
  JOIN court ct ON ct.id = c.court_id

UNION ALL
SELECT c.code, 'Судья', j.full_name
  FROM c
  JOIN case_judge cj ON cj.case_id = c.id
  JOIN judge j       ON j.id = cj.judge_id

UNION ALL
SELECT
    c.code,
    'Сторона (' ||
    CASE s.type::text
        WHEN 'PLAINTIFF' THEN 'Истец'
        WHEN 'DEFENDANT' THEN 'Ответчик'
        ELSE 'Другое'
    END || ')',
    s.full_name
  FROM c
  JOIN case_side cs ON cs.case_id = c.id
  JOIN side s       ON s.id = cs.side_id

ORDER BY case_code, entity, value;


-- ============================================================================
-- Запрос 3. Все карточки со судом и судьёй — обзор того, что вообще есть в БД.
-- ============================================================================
SELECT
    c.id,
    c.uid,
    c.code,
    c.status,
    j.full_name  AS judge,
    ct.name      AS court,
    ct.code      AS court_code
FROM "case" c
JOIN court ct           ON ct.id = c.court_id
LEFT JOIN case_judge cj ON cj.case_id = c.id
LEFT JOIN judge j       ON j.id = cj.judge_id
ORDER BY c.id;
