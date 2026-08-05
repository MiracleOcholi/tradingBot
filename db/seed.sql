-- Maverick — seed (one-time apply, same session as schema.sql).
-- ON CONFLICT DO NOTHING so dashboard-edited config survives redeploys/re-runs.

insert into public.config (id) values (1)
on conflict (id) do nothing;

-- Key-rotation cadence: every 3.5 months from 2026-08-05.
-- Seeded chain: 2026-11-20 → 2027-03-05 → 2027-06-20 → 2027-10-05 → 2028-01-20.
insert into public.reminders (name, next_due) values
  ('deriv_key_rotation', '2026-11-20')
on conflict (name) do nothing;

insert into public.engine_state (symbol) values
  ('R_10'), ('R_50'), ('R_75'), ('1HZ150V'), ('JD10'), ('JD75'), ('JD100')
on conflict (symbol) do nothing;
