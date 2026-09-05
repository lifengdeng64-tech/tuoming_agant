# ruff: noqa: E501

APP_STYLES = """
<style>
:root {
    --ink: #1d1d1f;
    --muted: #86868b;
    --line: rgba(29, 29, 31, .09);
    --surface: rgba(255, 255, 255, .92);
    --canvas: #f5f5f7;
    --blue: #0071e3;
    --blue-soft: #eaf4ff;
    --green: #248a3d;
    --radius: 20px;
    --shadow: 0 12px 34px rgba(0, 0, 0, .055);
}
html, body, [class*="css"] {
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "PingFang SC",
        "Microsoft YaHei", sans-serif;
    letter-spacing: 0 !important;
}
.stApp { background: var(--canvas); color: var(--ink); }
[data-testid="stHeader"] {
    background: rgba(245, 245, 247, .82);
    backdrop-filter: saturate(180%) blur(18px);
}
[data-testid="stSidebar"] {
    background: rgba(255, 255, 255, .72);
    border-right: 1px solid var(--line);
}
.block-container { max-width: 1480px; padding-top: 1.75rem; padding-bottom: 4rem; }
h1, h2, h3 { color: var(--ink); letter-spacing: -.025em !important; }
h1 { font-size: 2rem !important; line-height: 1.12 !important; font-weight: 700 !important; }
h2 { font-size: 1.35rem !important; }
h3 { font-size: 1.05rem !important; }
p, label, [data-testid="stCaptionContainer"] { letter-spacing: 0 !important; }
.brand-lockup { display: flex; align-items: center; gap: .75rem; margin: .35rem 0 1.75rem; }
.brand-mark {
    width: 38px; height: 38px; display: grid; place-items: center; border-radius: 12px;
    background: linear-gradient(145deg, #147ce5, #0064cf); color: white;
    box-shadow: 0 8px 18px rgba(0, 113, 227, .22); font-weight: 750;
}
.brand-lockup strong, .brand-lockup span { display: block; line-height: 1.25; }
.brand-lockup strong { color: var(--ink); font-size: 1rem; }
.brand-lockup span { color: var(--muted); font-size: .73rem; margin-top: .14rem; }
.sidebar-status { display: grid; grid-template-columns: 1fr auto; gap: .48rem .8rem; margin-bottom: 1.5rem; font-size: .78rem; }
.sidebar-status span { color: var(--muted); }
.sidebar-status strong { color: var(--ink); font-weight: 650; text-align: right; }
.security-state { border: 1px solid rgba(36, 138, 61, .13); background: rgba(233, 248, 237, .72); border-radius: 14px; padding: .72rem .85rem; }
.security-state span, .security-state small { display: block; }
.security-state span { color: var(--green); font-size: .82rem; font-weight: 650; }
.security-state small { color: var(--muted); font-size: .73rem; margin-top: .2rem; }
.security-state i { display: inline-block; width: 7px; height: 7px; border-radius: 50%; background: #34c759; margin-right: .28rem; box-shadow: 0 0 0 4px rgba(52, 199, 89, .12); }
[data-testid="stMetric"], [data-testid="stVerticalBlockBorderWrapper"] {
    background: var(--surface); border-color: var(--line) !important;
    border-radius: var(--radius) !important; box-shadow: var(--shadow);
}
[data-testid="stMetric"] { min-height: 102px; padding: 1rem 1.15rem; }
[data-testid="stMetricValue"] { color: var(--ink); font-size: 1.55rem; letter-spacing: -.035em; }
[data-testid="stMetricLabel"] { color: var(--muted); }
[data-testid="stSegmentedControl"] { margin: .7rem 0 1.55rem; }
[data-testid="stSegmentedControl"] [role="radiogroup"] { background: rgba(118, 118, 128, .10); border: 1px solid rgba(118, 118, 128, .08); border-radius: 13px; padding: 4px; }
[data-testid="stSegmentedControl"] label { border-radius: 10px !important; }
.stButton > button, .stDownloadButton > button { border-radius: 12px; min-height: 2.5rem; border-color: rgba(29, 29, 31, .13); }
.stButton > button[kind="primary"] { background: var(--blue); border-color: var(--blue); }
.stButton > button[kind="primary"]:hover { background: #0077ed; border-color: #0077ed; }
[data-baseweb="select"] > div, [data-baseweb="input"] > div { border-radius: 12px !important; border-color: rgba(29, 29, 31, .12) !important; background: rgba(255,255,255,.88) !important; }
[data-testid="stFileUploaderDropzone"] { background: var(--surface); border: 1px dashed rgba(0, 113, 227, .35); border-radius: var(--radius); min-height: 134px; }
[data-testid="stExpander"] { border: 1px solid var(--line); border-radius: 16px; background: var(--surface); overflow: hidden; }
[data-testid="stDataFrame"] { border: 1px solid var(--line); border-radius: 16px; overflow: hidden; }
[data-testid="stChatMessage"] { background: var(--surface); border: 1px solid var(--line); border-radius: 18px; padding: .8rem 1rem; margin-bottom: .65rem; box-shadow: 0 5px 18px rgba(0,0,0,.025); }
[data-testid="stChatInput"] { border-color: rgba(29,29,31,.13); border-radius: 16px; }
.section-meta { color: var(--muted); font-size: .78rem; text-align: right; }
.page-intro { max-width: 760px; padding: .6rem 0 1.35rem; }
.page-intro span, .eyebrow { color: var(--blue); font-size: .72rem; font-weight: 750; letter-spacing: .12em !important; }
.page-intro h2 { font-size: 2.1rem !important; line-height: 1.12; margin: .38rem 0 .55rem; }
.page-intro p { color: var(--muted); font-size: .98rem; margin: 0; line-height: 1.55; }
.insight-card { min-height: 126px; padding: 1.15rem 1.2rem; border: 1px solid var(--line); border-radius: var(--radius); background: var(--surface); box-shadow: var(--shadow); }
.insight-card span, .insight-card strong, .insight-card small { display: block; }
.insight-card span { color: var(--muted); font-size: .78rem; }
.insight-card strong { color: var(--ink); font-size: 1.65rem; letter-spacing: -.04em; margin: .4rem 0 .28rem; }
.insight-card small { color: var(--blue); font-size: .72rem; }
.dashboard-hero { display: flex; align-items: center; justify-content: space-between; gap: 1.4rem; margin: .25rem 0 1.1rem; padding: 1.35rem 1.5rem; border: 1px solid rgba(0,113,227,.12); border-radius: 22px; background: radial-gradient(circle at 92% 15%, rgba(90,200,250,.18), transparent 31%), linear-gradient(135deg, rgba(255,255,255,.96), rgba(234,244,255,.88)); box-shadow: var(--shadow); }
.dashboard-hero span { color: var(--blue); font-size: .68rem; font-weight: 760; letter-spacing: .12em !important; }
.dashboard-hero h3 { margin: .28rem 0 .32rem; font-size: 1.18rem !important; }
.dashboard-hero p { margin: 0; color: var(--muted); font-size: .84rem; }
.dashboard-badges { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: .45rem; }
.dashboard-badges b, .chart-kind { display: inline-flex; align-items: center; width: fit-content; color: #0064cf; background: rgba(0,113,227,.085); border: 1px solid rgba(0,113,227,.1); border-radius: 999px; padding: .35rem .62rem; font-size: .68rem; font-weight: 680; white-space: nowrap; }
.chart-kind { margin: .15rem 0 -.35rem; }
[class*="st-key-dashboard-charts"] [data-testid="stVerticalBlockBorderWrapper"] { min-height: 452px; overflow: hidden; background: linear-gradient(180deg, rgba(255,255,255,.98), rgba(250,250,252,.92)); }
[class*="st-key-dashboard-charts"] [data-testid="stPlotlyChart"] { border-radius: 16px; overflow: hidden; }
.empty-state { min-height: 168px; display: grid; place-content: center; justify-items: center; gap: .65rem; color: var(--muted); background: rgba(255,255,255,.52); border: 1px dashed rgba(29,29,31,.14); border-radius: var(--radius); }
.empty-state strong { width: 38px; height: 38px; display: grid; place-items: center; border-radius: 12px; color: var(--blue); background: var(--blue-soft); font-size: .82rem; }
.empty-state span { font-size: .86rem; }
.chat-empty { color: var(--muted); text-align: center; padding: 2.5rem 1rem; border-bottom: 1px solid var(--line); font-size: .86rem; }
.onboarding-hero { max-width: 920px; margin: 3.2rem auto 2.2rem; display: flex; align-items: center; gap: 1.25rem; padding: 1.8rem; border: 1px solid var(--line); border-radius: 24px; background: linear-gradient(135deg, #fff 0%, #edf6ff 100%); box-shadow: var(--shadow); }
.onboarding-mark { width: 56px; height: 56px; border-radius: 16px; font-size: 1.35rem; flex: none; }
.onboarding-hero h1 { margin: .2rem 0 .35rem; font-size: 1.9rem !important; }
.onboarding-hero p { margin: 0; color: var(--muted); }
[data-testid="stTextInput"] input[type="password"] { letter-spacing: .12em !important; }
@media (max-width: 900px) {
    .block-container { padding: 1.1rem .85rem 5rem; }
    h1 { font-size: 1.55rem !important; }
    .page-intro h2 { font-size: 1.7rem !important; }
    [class*="st-key-dashboard-charts"] [data-testid="stHorizontalBlock"] {
        flex-wrap: wrap;
    }
    [class*="st-key-dashboard-charts"] [data-testid="stColumn"] {
        min-width: 100% !important;
        flex: 1 1 100% !important;
    }
    [data-testid="stMetric"] { min-height: 88px; padding: .75rem .85rem; }
    [data-testid="stMetricValue"] { font-size: 1.25rem; }
    .insight-card { min-height: 112px; }
    .security-state { margin-top: .2rem; }
    .section-meta { text-align: left; }
    [data-testid="stSegmentedControl"] [role="radiogroup"] { overflow-x: auto; }
    .onboarding-hero { margin-top: 1rem; align-items: flex-start; padding: 1.25rem; }
    .dashboard-hero { align-items: flex-start; flex-direction: column; padding: 1.05rem; }
    .dashboard-badges { justify-content: flex-start; }
}
</style>
"""
