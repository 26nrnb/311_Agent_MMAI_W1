-- Ticket-store columns from the data pipeline and a tool error code on the
-- event log (311 Interfaces doc, sections 5 and 7). Safe to re-run.
-- RLS stays on with no policies: server and dashboard only.

-- tickets: the pipeline's TICKET_SCHEMA columns the table lacked.
alter table tickets add column if not exists street text;   -- street only, for nearby search
alter table tickets add column if not exists x_m integer;   -- position on the fictional city grid, metres,
alter table tickets add column if not exists y_m integer;   -- so radius_m duplicate windows can work
alter table tickets add column if not exists source text not null default 'agent';
alter table tickets drop constraint if exists tickets_source_check;
alter table tickets add constraint tickets_source_check check (source in ('seed', 'fixture', 'agent'));
create index if not exists tickets_grid on tickets (code, x_m, y_m) where x_m is not null;

comment on column tickets.street is 'Street only, without the house number (pipeline: street).';
comment on column tickets.x_m is 'Fictional city grid, metres; null for agent-created tickets.';
comment on column tickets.source is 'seed | fixture (data pipeline) | agent (created by create_ticket).';

-- interaction_events: a tool error code, so failures can be counted without parsing text.
alter table interaction_events add column if not exists error_code text;
comment on column interaction_events.error_code is 'Tool error code (Interfaces doc section 3); null when none.';

-- Agent-created ticket IDs start above every seeded range: seeds 311-YYYY-100000 up,
-- fixtures 001045 to 001048, re-run simulations 900001 up (src/agent/rerun.py).
-- src/tools/store.py FIRST_AGENT_TICKET must match.
select setval('ticket_seq', greatest((select last_value from ticket_seq), 500000));
