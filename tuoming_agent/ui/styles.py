APP_STYLES = """
<style>
:root {
    --ink: #17211b;
    --muted: #69766e;
    --line: #dbe2dd;
    --surface: #ffffff;
    --canvas: #f6f8f6;
    --green: #176b4d;
    --green-soft: #e8f3ed;
    --coral: #c75b43;
    --blue: #32678f;
}
html, body, [class*="css"] { letter-spacing: 0 !important; }
.stApp { background: var(--canvas); color: var(--ink); }
[data-testid="stHeader"] { background: rgba(246, 248, 246, .96); }
[data-testid="stSidebar"] {
    background: #edf2ee;
    border-right: 1px solid #d4ddd6;
}
.block-container {
    max-width: 1420px;
    padding-top: 1.7rem;
    padding-bottom: 4rem;
}
h1 { font-size: 1.72rem !important; line-height: 1.2 !important; color: var(--ink); }
h2 { font-size: 1.15rem !important; color: var(--ink); }
h3 { font-size: 1rem !important; color: var(--ink); }
p, label, [data-testid="stCaptionContainer"] { letter-spacing: 0 !important; }
.brand-lockup {
    display: flex;
    align-items: center;
    gap: .7rem;
    margin: .35rem 0 1.7rem;
}
.brand-mark {
    width: 34px;
    height: 34px;
    display: grid;
    place-items: center;
    border-radius: 6px;
    background: var(--green);
    color: white;
    font-weight: 750;
    font-size: 1rem;
}
.brand-lockup strong, .brand-lockup span { display: block; line-height: 1.25; }
.brand-lockup strong { color: var(--ink); font-size: .98rem; }
.brand-lockup span { color: var(--muted); font-size: .76rem; margin-top: .12rem; }
.sidebar-status {
    display: grid;
    grid-template-columns: 1fr auto;
    gap: .48rem .8rem;
    margin-bottom: 1.5rem;
    font-size: .78rem;
}
.sidebar-status span { color: var(--muted); }
.sidebar-status strong { color: var(--ink); font-weight: 650; text-align: right; }
.security-state {
    border-left: 3px solid var(--green);
    padding: .5rem 0 .5rem .85rem;
}
.security-state span, .security-state small { display: block; }
.security-state span { color: var(--green); font-size: .84rem; font-weight: 650; }
.security-state small { color: var(--muted); font-size: .76rem; margin-top: .24rem; }
.security-state i {
    display: inline-block;
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: var(--green);
    margin-right: .28rem;
}
[data-testid="stMetric"] {
    min-height: 92px;
    background: var(--surface);
    border-color: var(--line) !important;
    border-radius: 6px;
    padding: .82rem 1rem;
    box-shadow: none;
}
[data-testid="stMetricValue"] { font-size: 1.42rem; color: var(--ink); }
[data-testid="stMetricLabel"] { color: var(--muted); }
[data-testid="stMetric"]:nth-of-type(2) [data-testid="stMetricValue"] { color: var(--blue); }
[data-testid="stMetric"]:nth-of-type(3) [data-testid="stMetricValue"] { color: var(--coral); }
[data-testid="stSegmentedControl"] { margin: .65rem 0 1.4rem; }
[data-testid="stSegmentedControl"] [role="radiogroup"] {
    background: #e9eeea;
    border: 1px solid #d8e0da;
    border-radius: 7px;
    padding: 3px;
}
[data-testid="stSegmentedControl"] label { border-radius: 5px !important; }
.stButton > button, .stDownloadButton > button { border-radius: 6px; min-height: 2.45rem; }
.stButton > button[kind="primary"] { background: var(--green); border-color: var(--green); }
.stButton > button[kind="primary"]:hover { background: #0f583e; border-color: #0f583e; }
[data-testid="stFileUploaderDropzone"] {
    background: var(--surface);
    border: 1px dashed #8da096;
    border-radius: 7px;
    min-height: 126px;
}
[data-testid="stExpander"] {
    border: 1px solid var(--line);
    border-radius: 6px;
    background: var(--surface);
}
[data-testid="stDataFrame"] { border: 1px solid var(--line); border-radius: 6px; overflow: hidden; }
[data-testid="stChatMessage"] {
    background: transparent;
    border-bottom: 1px solid var(--line);
    border-radius: 0;
    padding-left: .25rem;
    padding-right: .25rem;
}
[data-testid="stChatInput"] { border-color: #aab8af; }
.section-meta { color: var(--muted); font-size: .78rem; text-align: right; }
.empty-state {
    min-height: 158px;
    display: grid;
    place-content: center;
    justify-items: center;
    gap: .55rem;
    color: var(--muted);
    background: #f1f4f2;
    border: 1px dashed #cbd5ce;
    border-radius: 6px;
}
.empty-state strong {
    width: 34px;
    height: 34px;
    display: grid;
    place-items: center;
    border-radius: 6px;
    color: var(--green);
    background: var(--green-soft);
    font-size: .78rem;
}
.empty-state span { font-size: .86rem; }
.chat-empty {
    color: var(--muted);
    text-align: center;
    padding: 2.5rem 1rem;
    border-bottom: 1px solid var(--line);
    font-size: .86rem;
}
@media (max-width: 768px) {
    .block-container { padding: 1.1rem .85rem 5rem; }
    h1 { font-size: 1.42rem !important; }
    [data-testid="stMetric"] { min-height: 82px; padding: .65rem .75rem; }
    [data-testid="stMetricValue"] { font-size: 1.18rem; }
    .security-state { margin-top: .2rem; }
    .section-meta { text-align: left; }
    [data-testid="stSegmentedControl"] [role="radiogroup"] { overflow-x: auto; }
}
</style>
"""
