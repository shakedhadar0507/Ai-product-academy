-- Garmin tables for Supabase. Paste into Supabase → SQL Editor → Run (once).
-- Row Level Security is on with no policies, so the public (publishable) key
-- can read nothing; only the server-side secret key can read or write.

create table if not exists garmin_activities (
    activity_id        bigint primary key,
    start_time_local   timestamp,
    type               text,
    name               text,
    distance_m         double precision,
    duration_s         double precision,
    moving_s           double precision,
    avg_pace_s_per_km  double precision,
    avg_hr             double precision,
    max_hr             double precision,
    elevation_gain_m   double precision,
    avg_cadence_spm    double precision,
    avg_power_w        double precision,
    aerobic_te         double precision,
    anaerobic_te       double precision,
    training_load      double precision,
    vo2max             double precision,
    avg_gct_ms         double precision,
    avg_vo_cm          double precision,
    avg_stride_m       double precision,
    avg_vertical_ratio double precision,
    avg_gct_balance    double precision
);

create table if not exists garmin_activity_laps (
    activity_id     bigint,
    lap_index       integer,
    start_time      text,
    distance_m      double precision,
    duration_s      double precision,
    avg_hr          double precision,
    max_hr          double precision,
    avg_speed_mps   double precision,
    avg_cadence_spm double precision,
    avg_power_w     double precision,
    avg_gct_ms      double precision,
    avg_vo_mm       double precision,
    avg_stride_mm   double precision,
    primary key (activity_id, lap_index)
);

create table if not exists garmin_daily_wellness (
    date                 date primary key,
    resting_hr           double precision,
    hrv_last_night_ms    double precision,
    hrv_weekly_avg_ms    double precision,
    hrv_status           text,
    sleep_score          double precision,
    sleep_s              double precision,
    deep_sleep_s         double precision,
    light_sleep_s        double precision,
    rem_sleep_s          double precision,
    awake_s              double precision,
    body_battery_high    double precision,
    body_battery_low     double precision,
    body_battery_charged double precision,
    stress_avg           double precision,
    steps                double precision,
    training_readiness   double precision,
    training_status      text,
    acute_load           double precision,
    chronic_load         double precision,
    vo2max_running       double precision
);

alter table garmin_activities enable row level security;
alter table garmin_activity_laps enable row level security;
alter table garmin_daily_wellness enable row level security;
