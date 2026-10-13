-- Run as an authorized database operator. Requests are not completed installs.
-- Historical records have source='unknown' and article=null.
select date_trunc('day', created_at at time zone 'UTC')::date as day,
       platform, source, article, count(*) as download_requests
from public.potluck_downloads
where created_at >= now() - interval '28 days'
group by 1, 2, 3, 4
order by 1 desc, 5 desc;
