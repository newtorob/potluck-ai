-- Applied to production: 20261007192450_potluck_download_attribution (2026-10-07).
-- Additive: existing platform-only RPC and historical rows keep working.
begin;
alter table public.potluck_downloads
  add column source text not null default 'unknown',
  add column article text,
  add constraint potluck_downloads_source_check check (source in
    ('unknown', 'direct', 'google', 'bing', 'duckduckgo', 'yahoo', 'x', 'reddit',
     'discord', 'email', 'paid-campaign', 'campaign', 'other-referral')),
  add constraint potluck_downloads_article_check check
    (article is null or (length(article) <= 160 and article ~ '^[a-z0-9]+(-[a-z0-9]+)*$'));

-- Anonymous callers can append only these bounded fields, never read rows.
-- SECURITY INVOKER keeps normal column privileges, RLS and constraints in force.
grant insert (platform, source, article) on public.potluck_downloads to anon;
create policy potluck_downloads_append on public.potluck_downloads
  for insert to anon with check (true);

create function public.potluck_log_attributed_download(
  p_platform text, p_source text, p_article text
) returns void language sql security invoker set search_path = '' as $$
  insert into public.potluck_downloads (platform, source, article)
  values (p_platform, p_source, p_article);
$$;
revoke all on function public.potluck_log_attributed_download(text, text, text) from public, authenticated;
grant execute on function public.potluck_log_attributed_download(text, text, text) to anon, service_role;
commit;
