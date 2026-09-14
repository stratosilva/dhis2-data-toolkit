import os
import json
import base64
from io import BytesIO
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns
from dhis2 import Api
from dhis2.exceptions import RequestException
from dhis2_env import api_from_env

# --- 1. Configuration & Setup ---
api = api_from_env('EIDSR')
PROGRAM_UID = 'JRuLW57woOB'
CSV_FILENAME = 'EBS_Zambia_Dataset.csv'

# Load the TEA mapping
with open('JRuLW57woOB-EBS_EMS-eIDSR_Zambia.json', 'r') as f:
    program_metadata = json.load(f)

tea_mapping = {}
for item in program_metadata['programTrackedEntityAttributes']:
    tea = item['trackedEntityAttribute']
    tea_mapping[tea['id']] = f"{tea['id']}_{tea['name']}"

# Load the Program Stage Mapping
with open('JRuLW57woOB_ProgramStages.json', 'r') as f:
    ps_metadata = json.load(f)

ps_mapping = {}
de_mapping = {}
all_ps_columns = []

for ps in ps_metadata['programStages']:
    ps_id = ps['id']
    ps_name = ps['name']
    ps_mapping[ps_id] = ps_name
    de_mapping[ps_id] = {}

    all_ps_columns.extend([f"{ps_name}_orgUnitUID", f"{ps_name}_occurredAt"])

    for psde in ps['programStageDataElements']:
        de = psde['dataElement']
        col_name = f"{ps_name}_{de['id']}_{de['name']}"
        de_mapping[ps_id][de['id']] = de['name']
        all_ps_columns.append(col_name)

try:
    ou_df = pd.read_csv('Org Units eIDSR Prod.csv')
    cols_to_keep = ['name', 'level']
    if 'path' in ou_df.columns:
        cols_to_keep.append('path')
    ou_dict = ou_df.set_index('id')[cols_to_keep].to_dict('index')
except FileNotFoundError:
    print("Warning: 'Org Units eIDSR Prod.csv' not found. Org Unit data will be limited.")
    ou_dict = {}


def get_base64_image(fig):
    buf = BytesIO()
    fig.savefig(buf, format='png', bbox_inches='tight')
    buf.seek(0)
    image_base64 = base64.b64encode(buf.getvalue()).decode('utf-8')
    plt.close(fig)
    return image_base64


# --- 2 & 3. Data Extraction & Transformation ---
if os.path.exists(CSV_FILENAME):
    print(f"Found existing file '{CSV_FILENAME}'. Skipping API extraction...")
    df = pd.read_csv(CSV_FILENAME, low_memory=False)
    df['updatedAt'] = pd.to_datetime(df['updatedAt'], errors='coerce')
    df['enrolledAt'] = pd.to_datetime(df['enrolledAt'], errors='coerce')
else:
    print(f"File '{CSV_FILENAME}' not found. Fetching enrollments from DHIS2...")
    all_data = []
    page = 1
    has_next_page = True

    while has_next_page:
        print(f"  Processing page {page}...")
        enrollments_response = api.get(
            'tracker/enrollments',
            params={'program': PROGRAM_UID, 'ouMode': 'ALL', 'enrolledAfter': '1900-01-01', 'fields': '*', 'page': page,
                    'pageSize': 50}
        ).json()

        instances = enrollments_response.get('instances', [])
        if not instances:
            break

        for enr in instances:
            row = {
                'updatedAt': enr.get('updatedAt'),
                'enrolledAt': enr.get('enrolledAt'),
                'trackedEntity': enr.get('trackedEntity'),
                'enrollment_id': enr.get('enrollment'),
                'orgUnit_id': enr.get('orgUnit')
            }
            notes = enr.get('notes', [])
            row['notes_value'] = notes[0]['value'] if notes else None
            all_data.append(row)

        if len(instances) < 50:
            has_next_page = False
        else:
            page += 1

    print(f"\nFetched {len(all_data)} enrollments. Now fetching nested TEIs, Enrollments, and Events...")

    valid_data = []
    for idx, row in enumerate(all_data):
        tei_uid = row['trackedEntity']
        enrollment_uid = row['enrollment_id']

        if idx % 50 == 0 and idx > 0:
            print(f"  Processed {idx}/{len(all_data)} tracked entities...")

        for tea_col in tea_mapping.values(): row[tea_col] = None
        for col in all_ps_columns: row[col] = None

        try:
            params = {"fields": "*"}
            tei_payload = api.get(f"tracker/trackedEntities/{tei_uid}", params=params).json()

            if 'trackedEntities' in tei_payload:
                tei_data = tei_payload['trackedEntities'][0] if len(tei_payload['trackedEntities']) > 0 else {}
            else:
                tei_data = tei_payload

            for attr in tei_data.get('attributes', []):
                if attr['attribute'] in tea_mapping:
                    row[tea_mapping[attr['attribute']]] = attr['value']

            for enr in tei_data.get('enrollments', []):
                if enr['enrollment'] == enrollment_uid:
                    for attr in enr.get('attributes', []):
                        if attr['attribute'] in tea_mapping:
                            row[tea_mapping[attr['attribute']]] = attr['value']

                    ps_events = {}
                    for event in enr.get('events', []):
                        ps_id = event.get('programStage')
                        if ps_id not in ps_events:
                            ps_events[ps_id] = []
                        ps_events[ps_id].append(event)

                    for ps_id, events_list in ps_events.items():
                        if ps_id not in ps_mapping:
                            continue
                        ps_name = ps_mapping[ps_id]

                        occurred_ats = [str(e.get('occurredAt', ''))[:10] for e in events_list if e.get('occurredAt')]
                        org_units = [str(e.get('orgUnit', '')) for e in events_list if e.get('orgUnit')]

                        if occurred_ats: row[f"{ps_name}_occurredAt"] = ";".join(occurred_ats)
                        if org_units: row[f"{ps_name}_orgUnitUID"] = ";".join(org_units)

                        for de_id, de_name in de_mapping[ps_id].items():
                            col_name = f"{ps_name}_{de_id}_{de_name}"
                            vals = []
                            for event in events_list:
                                val = next(
                                    (dv['value'] for dv in event.get('dataValues', []) if dv['dataElement'] == de_id),
                                    "")
                                vals.append(str(val) if val is not None else "")

                            if any(v != "" for v in vals):
                                row[col_name] = ";".join(vals)

            valid_data.append(row)
        except RequestException:
            valid_data.append(row)
        except Exception:
            valid_data.append(row)

    df = pd.DataFrame(valid_data)
    df['updatedAt'] = pd.to_datetime(df['updatedAt'])
    df['enrolledAt'] = pd.to_datetime(df['enrolledAt'])
    df.to_csv(CSV_FILENAME, index=False)
    print(f"\nSuccessfully extracted {len(df)} valid records. Saved to {CSV_FILENAME}")


def get_ou_info(ou_id):
    level, name, prov_name = None, None, None
    if ou_id in ou_dict:
        level = ou_dict[ou_id].get('level')
        name = ou_dict[ou_id].get('name')
        path = str(ou_dict[ou_id].get('path', ''))
        parts = path.split('/')
        if len(parts) > 2:
            prov_uid = parts[2]
            if prov_uid in ou_dict:
                prov_name = ou_dict[prov_uid]['name']
                if prov_name == "Northwestern":
                    prov_name = "North-Western"
    return pd.Series([level, name, prov_name])


if 'orgUnit_id' in df.columns and ou_dict:
    df[['orgUnit_level', 'orgUnit_name', 'province_name']] = df['orgUnit_id'].apply(get_ou_info)

# --- Define Specific Target Columns ---
col_entry_type = tea_mapping.get('wILqVg7z3wl')
col_verify_status = tea_mapping.get('BgAqKOVZteZ')
col_signal_source = tea_mapping.get('Jm1UC1jzuJr')
col_event_nature = tea_mapping.get('NY3286d2tSr')
col_animal_group = tea_mapping.get('z8R7FRE6fbc')
col_animal_type = tea_mapping.get('owKzsNpmgen')
col_animal_spec = tea_mapping.get('SOOVWpljeXx')
col_age = tea_mapping.get('YFbwvSZxiLb')
col_sex = tea_mapping.get('x6knMBF0HSC')
disease_col = tea_mapping.get('ulJKIfQyYoA')

# --- Print Audit UIDs to Terminal ---
print("\n--- AUDIT: TEIs with Notes vs Structured Data Issues ---")
print("1. Missing Age/Sex: izWnwlXLLbS")
print("2. Missing Animal Type: TvMBPC9YA4q")
print("3. Age Inconsistency (11 vs 12): Gzrah2fNiDG")
print("4. Age Inconsistency (22 vs 13): IPjPkYridsq")
print("5. Missing Lab Data: R11mO7pyyO8")
print("6. Bad Practice (Not Public Health): Nc2JXgHemfN")
print("--------------------------------------------------------\n")

# --- HTML REPORT GENERATION ---
print("Generating charts and HTML report...")
sns.set_theme(style="whitegrid")

# Initialize the main HTML string
html_content = f"""
<html>
<head>
    <title>EBS Zambia - Data Analysis Report</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 40px; color: #333; }}
        h1, h2 {{ color: #2C3E50; border-bottom: 2px solid #eee; padding-bottom: 5px; margin-top: 50px; }}
        .chart-container {{ margin-bottom: 50px; display: flex; flex-direction: column; align-items: flex-start; }}
        table {{ border-collapse: collapse; width: 80%; margin-bottom: 30px; font-size: 14px; }}
        th, td {{ border: 1px solid #ddd; padding: 10px; text-align: left; }}
        th {{ background-color: #f2f2f2; }}
        .alert-box {{ background-color: #ffebe6; border-left: 5px solid #ff4d4d; padding: 15px; margin: 20px 0; width: 80%; }}
        .alert-box h3 {{ margin-top: 0; color: #cc0000; }}
        .flex-row {{ display: flex; gap: 40px; width: 100%; flex-wrap: wrap; }}
        .flex-col {{ flex: 1; min-width: 300px; }}
        .audit-table th {{ background-color: #e9ecef; border: 1px solid #dee2e6; }}
        .audit-table td {{ border: 1px solid #dee2e6; }}
    </style>
</head>
<body>
    <h1>Event Management Surveillance (EBS) - Zambia Report</h1>
    <p>Total valid events analyzed: <strong>{len(df)}</strong></p>
"""

# =====================================================================
# PART 1: SYSTEM USAGE & METADATA ASSESSMENT
# =====================================================================

# 1. Org Unit Distribution
html_org_unit = "<h3>1. Org Unit Distribution</h3>"
if 'orgUnit_level' in df.columns:
    ou_level_names = {1: "National", 2: "Province", 3: "District", 4: "Constituency", 5: "Ward", 6: "Facility",
                      7: "Level 7", 8: "Level 8", 9: "Level 9"}
    level_counts = df['orgUnit_level'].map(ou_level_names).fillna('Unknown').value_counts(dropna=False)
    fig, ax = plt.subplots(figsize=(8, 5))
    sns.barplot(x=level_counts.index.astype(str), y=level_counts.values, ax=ax, palette="viridis")
    ax.set_title('Event Distribution by Org Unit Level')
    html_org_unit += f"""<div class="chart-container"><img src="data:image/png;base64,{get_base64_image(fig)}"></div>"""

# 2. Data Completeness (TEA Usage)
html_tea_usage = "<h3>2. Data Completeness (TEA Usage)</h3>"
tea_columns = [col for col in df.columns if len(col.split('_')[0]) == 11 and col in tea_mapping.values()]
if tea_columns:
    tea_usage = (df[tea_columns].notna().mean() * 100).sort_values(ascending=False).head(15)
    fig, ax = plt.subplots(figsize=(10, 6))
    sns.barplot(x=tea_usage.values, y=[c.split('_')[1] for c in tea_usage.index], ax=ax, palette="mako")
    ax.set_title('Top 15 Most Used Attributes (Completion %)')
    ax.set_xlabel('Percentage of Events with Data (%)')
    html_tea_usage += f"""<div class="chart-container"><img src="data:image/png;base64,{get_base64_image(fig)}"></div>"""


# 3. Program Stage Usage & Delays
def get_first_date(date_str):
    if pd.isna(date_str) or not str(date_str).strip(): return pd.NaT
    dates = pd.to_datetime(str(date_str).split(';'), errors='coerce')
    valid_dates = dates.dropna()
    return valid_dates.min() if len(valid_dates) > 0 else pd.NaT


def clean_de_name(col_name, ps_name):
    parts = col_name.replace(f"{ps_name}_", "").split("_", 1)
    return parts[1] if len(parts) > 1 else col_name


html_ps_usage = "<h3>3. Program Stage Usage by Entry Type & Completeness</h3>"
html_ps_usage += "<div class='flex-row' style='align-items: flex-end;'>"

if col_entry_type in df.columns:
    df['clean_entry'] = df[col_entry_type].str.replace('ENTRY_TYPE_', '', regex=False).fillna('Missing')
else:
    df['clean_entry'] = 'Missing'

categories = {
    'EVENT': df[df['clean_entry'] == 'EVENT'],
    'SIGNAL': df[df['clean_entry'] == 'SIGNAL'],
    'OTHER / MISSING': df[~df['clean_entry'].isin(['EVENT', 'SIGNAL'])]
}

for cat_name, subset_df in categories.items():
    if len(subset_df) == 0: continue
    cat_usage_data = []
    for ps_name in ps_mapping.values():
        ps_occ_col = f"{ps_name}_occurredAt"
        if ps_occ_col not in subset_df.columns: continue
        ps_reported = subset_df[subset_df[ps_occ_col].notna() & (subset_df[ps_occ_col] != '')]
        usage_perc = (len(ps_reported) / len(subset_df)) * 100
        cat_usage_data.append({'Program Stage': ps_name, 'Usage (%)': usage_perc})

    if cat_usage_data:
        cat_usage_df = pd.DataFrame(cat_usage_data)
        fig, ax = plt.subplots(figsize=(6, 4))
        sns.barplot(x='Usage (%)', y='Program Stage', data=cat_usage_df, ax=ax, palette="cubehelix")
        ax.set_title(f"Usage for '{cat_name}' (Total n={len(subset_df)})")
        ax.set_xlim(0, 100)
        html_ps_usage += f"<div class='flex-col'><img src='data:image/png;base64,{get_base64_image(fig)}'></div>"
html_ps_usage += "</div>"

ps_delay_data = []
top_des_html = "<div class='flex-row'>"
for ps_name in ps_mapping.values():
    ps_occ_col = f"{ps_name}_occurredAt"
    if ps_occ_col not in df.columns: continue
    ps_reported = df[df[ps_occ_col].notna() & (df[ps_occ_col] != '')].copy()

    if len(ps_reported) > 0:
        de_cols = [c for c in ps_reported.columns if
                   c.startswith(f"{ps_name}_") and c not in [ps_occ_col, f"{ps_name}_orgUnitUID",
                                                             f"{ps_name}_first_occurredAt", f"{ps_name}_delay_days"]]
        if de_cols:
            de_comp = (ps_reported[de_cols].apply(lambda x: x.notna() & (x != '')).mean() * 100).sort_values(
                ascending=False)
            if ps_name == "Risk Assessment":
                title_text = f"All Fields: '{ps_name}'"
            else:
                de_comp = de_comp.head(10)
                title_text = f"Top 10 Fields: '{ps_name}'"
            de_comp_df = de_comp.reset_index()
            de_comp_df.columns = ['Data Element', 'Completion Rate (%)']
            de_comp_df['Data Element'] = de_comp_df['Data Element'].apply(lambda x: clean_de_name(x, ps_name))
            top_des_html += f"<div class='flex-col'><h4>{title_text}</h4>{de_comp_df.round(1).to_html(index=False)}</div>"

        if 'enrolledAt' in df.columns:
            ps_reported[f'{ps_name}_first_occurredAt'] = ps_reported[ps_occ_col].apply(get_first_date)
            ps_reported_dates = ps_reported.dropna(subset=['enrolledAt', f'{ps_name}_first_occurredAt'])
            if not ps_reported_dates.empty:
                delays = (ps_reported_dates[f'{ps_name}_first_occurredAt'] - ps_reported_dates['enrolledAt']).dt.days
                delays = delays[delays >= 0]
                avg_delay = delays.mean() if not delays.empty else 0
                median_delay = delays.median() if not delays.empty else 0
                ps_delay_data.append({'Program Stage': ps_name, 'Avg Delay (Days)': round(avg_delay, 1),
                                      'Median Delay (Days)': round(median_delay, 1)})
            else:
                ps_delay_data.append(
                    {'Program Stage': ps_name, 'Avg Delay (Days)': 'N/A', 'Median Delay (Days)': 'N/A'})
top_des_html += "</div>"
html_ps_usage += f"<div class='chart-container' style='width: 100%;'><h4>Data Completeness per Program Stage</h4>{top_des_html}</div>"

# =====================================================================
# PART 2: DATA QUALITY & WORKFLOW COMPLIANCE
# =====================================================================

# 4. Entry Types & Workflow Logic
html_workflow_logic = "<h3>4. Entry Types & Workflow Logic</h3>"
if col_entry_type in df.columns and col_verify_status in df.columns:
    df['clean_status'] = df[col_verify_status].str.replace('VERIFICATION_STATUS_', '', regex=False).fillna('Missing')
    wrong_signals = df[(df['clean_entry'] == 'SIGNAL') & (df['clean_status'] == 'VERIFIED')]
    wrong_events = df[(df['clean_entry'] == 'EVENT') & (df['clean_status'] != 'VERIFIED')]

    entry_counts = df['clean_entry'].value_counts(normalize=True) * 100
    status_counts = df['clean_status'].value_counts(normalize=True) * 100

    fig, axes = plt.subplots(1, 2, figsize=(14, 5))
    sns.barplot(x=entry_counts.index, y=entry_counts.values, ax=axes[0], palette="Blues")
    axes[0].set_title('Distribution: Type of Entry (%)')

    sns.barplot(x=status_counts.index, y=status_counts.values, ax=axes[1], palette="Greens")
    axes[1].set_title('Distribution: Verification Status (%)')

    html_workflow_logic += f"""
    <div class="chart-container">
        <img src="data:image/png;base64,{get_base64_image(fig)}">
        <div class="alert-box">
            <h3>Data Quality Workflow Alerts</h3>
            <ul>
                <li><strong>Signals incorrectly marked as Verified:</strong> {len(wrong_signals)} records. <em>(Signals should be Pending)</em></li>
                <li><strong>Events lacking Verified status:</strong> {len(wrong_events)} records. <em>(Events must always be Verified. Includes Pending, Not an Event, or Blank fields)</em></li>
            </ul>
        </div>
    </div>
    """

# 5. Reporting Delays
html_ps_delays = ""
if ps_delay_data:
    ps_delay_df = pd.DataFrame(ps_delay_data)
    html_ps_delays += f"""
    <div class="chart-container">
        <h3>5. Reporting Delays (Enrollment to First Event)</h3>
        <p>Measures the average time it takes for a user to input a program stage after the initial event is enrolled.</p>
        {ps_delay_df.to_html(index=False)}
    </div>
    """

# 6. Notes Utilization
html_notes_utilization = "<h3>6. Notes Utilization and Quality Assessment</h3>"
if 'notes_value' in df.columns:
    notes_present = df['notes_value'].replace(r'^\s*$', pd.NA, regex=True).notna()
    notes_count = notes_present.sum()
    total_enrollments = len(df)
    notes_perc = (notes_count / total_enrollments * 100) if total_enrollments > 0 else 0

    html_notes_utilization += f"""
    <div class="chart-container" style="width: 100%;">
        <p><strong>Notes Utilization:</strong> Out of {total_enrollments} total enrollments, free-text notes were provided in <strong>{notes_count}</strong> cases (<strong>{notes_perc:.1f}%</strong>).</p>
        <div style="display: flex; gap: 20px; width: 100%;">
            <div style="flex: 1; background-color: #e6ffe6; padding: 15px; border-left: 5px solid #28a745;">
                <h4 style="color: #218838; margin-top: 0;">Good Notes (High Epidemiological Value)</h4>
                <ul style="font-size: 14px;">
                    <li><em>"Suspected Food Poisoning Incident... Lufwanyama District Hospital recorded suspected Food Poisoning cases... consumed Munkoyo at Jehovah Witness church gathering... All 10 patients admitted... IV Fluids, ORS... communicated to Kalulushi Surveillance Officer for follow-up..."</em></li>
                    <li><em>"In Chipata, ELAMU office reported that wild Grey Herons have been dying since last week. A total of 18 birds have been confirmed dead so far. The affected birds inhabit large trees around the NAPSA building in town. A team of Wildlife Protection Officers, investigators, and veterinarians... visited the site... Samples collected and submitted to CVRI..."</em></li>
                    <li><em>"Following heavy rainfall in Lufwanyama District, a key bridge along the main access route to Chipese Health Post has been washed away... Delivery of essential medicines, vaccines and medical supplies is interrupted."</em></li>
                    <li><em>"Students from Chilanga college presented starte presenting with diarrhoea and vomoting at Mt eugenia Hospital on 2nd February 2026. Cholera rdt was tested and came out negative. Inspections where done at the school, it was discovered water not treated. Online chlorinator recommended and installed."</em></li>
                    <li><em>"Ostrede shanambe female aged 1 year 5 five months was admitted on 17/01/26 due to diarrhoea non blood... The family use a home based tap running water as their only water source at the household level and thre were no any travelling history of the patient in the past two weeks."</em></li>
                </ul>
            </div>
            <div style="flex: 1; background-color: #ffe6e6; padding: 15px; border-left: 5px solid #dc3545;">
                <h4 style="color: #c82333; margin-top: 0;">Bad Notes (Poor Practice & Redundant)</h4>
                <ul style="font-size: 14px;">
                    <li><em>"1 woman was punched by her brother on the head and sustained a cut while the other woman who is also pregnant was beaten by her brother with a stick."</em> (Not a public health concern).</li>
                    <li><em>"the case index resulted in death"</em> (Redundant: Use the structured outcome fields).</li>
                    <li><em>"not an event"</em> (Too brief: Provides no epidemiological justification).</li>
                    <li><em>"samples collected for analysis"</em> (Too vague: Lacks context on samples and lab).</li>
                    <li><em>"case was RDT negative"</em> (Redundant: Use the structured Lab Test/Assay TEAs).</li>
                </ul>
            </div>
        </div>
    </div>
    """

# 7. Notes vs Structured Data Audit
html_notes_audit = """
    <h3>7. Notes vs. Structured Data Audit</h3>
    <div class="chart-container" style="width: 100%;">
        <p>A common data quality issue in EBS reporting occurs when field officers embed crucial data points (like Age, Sex, or Event Source) into the free-text Notes field, rather than using the dedicated TEAs. This makes it impossible to query or aggregate the data effectively.</p>
        <p>Below is an illustrative audit using <strong>real instances from the Zambia DHIS2 dataset</strong> where data was misreported in the Notes field, causing either <strong>Missing Structured Data</strong> (TEAs left empty) or <strong>Inconsistent Data</strong> (TEAs contradict the note).</p>
        <table class="audit-table" style="width: 100%;">
            <thead>
                <tr>
                    <th>Scenario</th>
                    <th>Text Entered in 'Notes'</th>
                    <th>Actual TEA Values in Record</th>
                    <th>Audit Finding</th>
                </tr>
            </thead>
            <tbody>
                <tr>
                    <td>Age & Sex Missing</td>
                    <td><em>"A man aged 26 was involved in an accident at his place of work. He was buried half way and sustained severe injuries in the underground tunnel"</em></td>
                    <td>Age of index case: <code>[Empty]</code><br>Sex of index case: <code>[Empty]</code></td>
                    <td><span style="color: #d9534f; font-weight: bold;">Data entered in notes but not provided in structured manner.</span></td>
                </tr>
                <tr>
                    <td>Animal Data Missing</td>
                    <td><em>"the two individuals were bitten by the same Cat ,the cat was a domestic cat,it attacked 3 people but only two were bitten."</em></td>
                    <td>Animal group: <code>[Empty]</code><br>Animal type: <code>[Empty]</code></td>
                    <td><span style="color: #d9534f; font-weight: bold;">Data entered in notes but not provided in structured manner.</span></td>
                </tr>
                <tr>
                    <td>Age Inconsistency</td>
                    <td><em>"lufwanyama district recorded a snake bite case involving an 11 years old female from mbalango area."</em></td>
                    <td>Age of index case: <code>12</code></td>
                    <td><span style="color: #f0ad4e; font-weight: bold;">Inconsistency of notes vs structured data.</span></td>
                </tr>
                <tr>
                    <td>Age Inconsistency</td>
                    <td><em>"on 19th july 2025 at approximately 14:00 hours, a tragic drowning incident occurred in the namwala river, resulting in the death of a 22-year-old male..."</em></td>
                    <td>Age of index case: <code>13</code></td>
                    <td><span style="color: #f0ad4e; font-weight: bold;">Inconsistency of notes vs structured data.</span></td>
                </tr>
                <tr>
                    <td>Lab Data Missing</td>
                    <td><em>"As at 18 hours on 25/2/23, there the index was the only suspect. sample taken to livingstone lab for confirmation"</em></td>
                    <td>Laboratory name: <code>[Empty]</code></td>
                    <td><span style="color: #d9534f; font-weight: bold;">Data entered in notes but not provided in structured manner.</span></td>
                </tr>
            </tbody>
        </table>
    </div>
"""

# =====================================================================
# PART 3: EPIDEMIOLOGICAL DATA & STATISTICS
# =====================================================================

# 8. Signal Sources & Event Nature
html_signal_sources = "<h3>8. Signal Sources & Event Nature</h3>"
if col_signal_source in df.columns:
    df['clean_source'] = df[col_signal_source].str.replace('REPORT_SOURCE_', '', regex=False).str.replace('REPORT_', '',
                                                                                                          regex=False).fillna(
        'Missing')
    source_perc = (df['clean_source'].value_counts(normalize=True) * 100).head(15)
    fig, ax = plt.subplots(figsize=(10, 6))
    sns.barplot(x=source_perc.values, y=source_perc.index, ax=ax, palette="magma")
    ax.set_title('Top Signal Sources (% of Total)')
    source_table = (df['clean_source'].value_counts(normalize=True) * 100).round(2).reset_index()
    source_table.columns = ['Signal Source', 'Percentage (%)']
    html_signal_sources += f"""<div class="chart-container"><img src="data:image/png;base64,{get_base64_image(fig)}">{source_table.to_html(index=False)}</div>"""

if col_event_nature in df.columns:
    df['clean_nature'] = df[col_event_nature].str.replace('EVENT_TYPE_', '', regex=False).fillna('Missing')
    nature_perc = (df['clean_nature'].value_counts(normalize=True) * 100).round(2).reset_index()
    nature_perc.columns = ['Event Nature', 'Percentage (%)']
    html_signal_sources += f"""<div class="chart-container"><h4>Distribution of Signal/Event Types</h4>{nature_perc.to_html(index=False)}</div>"""

# 9. Animal Data Completion Checks
html_animal_checks = "<h3>9. Animal Data Completion Checks</h3>"
if col_event_nature in df.columns:
    animal_target_df = df[df[col_event_nature].isin(
        ['EVENT_TYPE_ANIMAL', 'EVENT_TYPE_HUMAN_ANIMAL', 'EVENT_TYPE_NON_HUMAN', 'EVENT_TYPE_HUMAN_NON'])]
    html_animal_checks += f"<p>Total records involving Animal or Human/Animal events: <strong>{len(animal_target_df)}</strong></p>"

    if len(animal_target_df) > 0:
        animal_cols_present = [c for c in [col_animal_group, col_animal_type, col_animal_spec] if c in df.columns]
        if animal_cols_present:
            clean_animal_df = animal_target_df[animal_cols_present].replace(r'^\s*$', pd.NA, regex=True).replace('None',
                                                                                                                 pd.NA)
            animal_comp = (clean_animal_df.notna().mean() * 100).round(2).reset_index()
            animal_comp.columns = ['Animal Data Field', 'Completion Rate (%)']
            animal_comp['Animal Data Field'] = animal_comp['Animal Data Field'].apply(
                lambda x: x.split('_', 1)[1] if '_' in x else x)
            html_animal_checks += f"<div class='chart-container'><p>When an event involves an animal, did the user fill out the required animal details?</p>{animal_comp.to_html(index=False)}</div>"
        else:
            html_animal_checks += "<p><em>No Animal TEA data columns were found.</em></p>"

# 10. Top 10 Diseases
html_top_diseases = "<h3>10. Top 10 Diseases/Syndromes</h3>"
disease_col = tea_mapping.get('ulJKIfQyYoA')
top_disease = None
if disease_col and disease_col in df.columns:
    top_diseases = df[disease_col].value_counts().head(10)
    if len(top_diseases) > 0:
        top_disease = top_diseases.index[0]
        fig, ax = plt.subplots(figsize=(10, 6))
        sns.barplot(x=top_diseases.values, y=top_diseases.index, ax=ax, palette="flare")
        ax.set_title('Top 10 Reported Diseases/Syndromes (Option Set)')
        html_top_diseases += f"""<div class="chart-container"><img src="data:image/png;base64,{get_base64_image(fig)}"></div>"""

# 11. Heatmap
html_heatmap = f"<h3>11. Heatmap of Top Disease: {top_disease}</h3>"
if top_disease and 'province_name' in df.columns:
    disease_df = df[df[disease_col] == top_disease]
    prov_counts = disease_df['province_name'].value_counts().reset_index()
    prov_counts.columns = ['Province', 'Event Count']
    try:
        import geopandas as gpd

        gdf = gpd.read_file('zm.json')
        if 'name' in gdf.columns:
            merged = gdf.merge(prov_counts, left_on='name', right_on='Province', how='left')
            merged['Event Count'] = merged['Event Count'].fillna(0)
            fig, ax = plt.subplots(1, 1, figsize=(10, 8))
            merged.plot(column='Event Count', cmap='OrRd', linewidth=0.8, ax=ax, edgecolor='0.5', legend=True)
            ax.axis('off')
            ax.set_title(f'Geographical Distribution: {top_disease}')
            html_heatmap += f"""<div class="chart-container"><img src="data:image/png;base64,{get_base64_image(fig)}"></div>"""
    except Exception:
        pass

# 12. Reporting Trend
html_time_trend = "<h3>12. Reporting Trend</h3>"
if 'enrolledAt' in df.columns:
    valid_dates = df.dropna(subset=['enrolledAt']).copy()
    valid_dates = valid_dates[valid_dates['enrolledAt'] >= '2022-01-01']
    if not valid_dates.empty:
        time_dist = valid_dates.set_index('enrolledAt').resample('M').size()
        fig, ax = plt.subplots(figsize=(12, 5))
        sns.lineplot(x=time_dist.index, y=time_dist.values, ax=ax, marker="o", color="b")
        ax.set_title('Event Reporting Distribution over Time (Monthly by Enrollment Date)')
        ax.set_xlabel('Reporting date')
        plt.xticks(rotation=45)
        html_time_trend += f"""<div class="chart-container"><p><em>Note: The chart starts from 2022 as there was no significant data reported before that year.</em></p><img src="data:image/png;base64,{get_base64_image(fig)}"></div>"""

# 13. Dynamic Outbreak Peak Analysis
html_oct_peak = "<h3>13. Outbreak Peak Analysis</h3>"
if 'enrolledAt' in df.columns and disease_col and disease_col in df.columns:
    valid_dates = df.dropna(subset=['enrolledAt']).copy()
    valid_dates = valid_dates[valid_dates['enrolledAt'] >= '2022-01-01']
    if not valid_dates.empty:
        # Dynamically find the exact month with the absolute maximum enrollments
        monthly_counts = valid_dates.set_index('enrolledAt').resample('M').size()
        peak_month_timestamp = monthly_counts.idxmax()
        peak_month_str = peak_month_timestamp.strftime('%B %Y')
        peak_count = monthly_counts.max()

        # Filter the original dataframe safely for that specific peak month
        start_date = peak_month_timestamp.replace(day=1).strftime('%Y-%m-%d')
        end_date = (peak_month_timestamp + pd.DateOffset(days=1)).replace(day=1).strftime('%Y-%m-%d')

        peak_df = df[(df['enrolledAt'] >= start_date) & (df['enrolledAt'] < end_date)]

        if not peak_df.empty:
            peak_disease_counts = peak_df[disease_col].value_counts().head(10)
            if len(peak_disease_counts) > 0:
                top_peak_disease = peak_disease_counts.index[0]
                top_peak_count = peak_disease_counts.values[0]
                top_peak_perc = (top_peak_count / len(peak_df[disease_col].dropna()) * 100).round(1)

                fig, ax = plt.subplots(figsize=(10, 6))
                sns.barplot(x=peak_disease_counts.values, y=peak_disease_counts.index, ax=ax, palette="Reds_r")
                ax.set_title(f'Top Diseases/Syndromes Reported in {peak_month_str}')
                ax.set_xlabel('Number of Events')

                peak_disease_summary = pd.DataFrame({
                    'Disease/Syndrome': peak_disease_counts.index,
                    'Count': peak_disease_counts.values,
                    'Percentage (%)': (peak_disease_counts.values / len(peak_df[disease_col].dropna()) * 100).round(2)
                })

                html_oct_peak += f"""
                <div class="chart-container">
                    <p>A significant spike occurred in <strong>{peak_month_str}</strong> (Total events: {peak_count}). The primary driver of this peak was an outbreak of <strong>{top_peak_disease}</strong>, accounting for <strong>{top_peak_perc}%</strong> of reports.</p>
                    <img src="data:image/png;base64,{get_base64_image(fig)}">
                    {peak_disease_summary.to_html(index=False)}
                </div>
                """

# --- Final Layout Construction ---
html_layout = f"""
    <h2 style="background-color: #2C3E50; color: white; padding: 10px;">PART 1: SYSTEM USAGE & METADATA ASSESSMENT</h2>
    {html_org_unit}
    {html_tea_usage}
    {html_ps_usage}

    <h2 style="background-color: #2C3E50; color: white; padding: 10px;">PART 2: DATA QUALITY & WORKFLOW COMPLIANCE</h2>
    {html_workflow_logic}
    {html_ps_delays}
    {html_notes_utilization}
    {html_notes_audit}

    <h2 style="background-color: #2C3E50; color: white; padding: 10px;">PART 3: EPIDEMIOLOGICAL DATA & STATISTICS</h2>
    {html_signal_sources}
    {html_animal_checks}
    {html_top_diseases}
    {html_heatmap}
    {html_time_trend}
    {html_oct_peak}
"""

html_content += html_layout + "</body></html>"

with open('EBS_Zambia_Report.html', 'w') as f:
    f.write(html_content)

print("\nReport successfully generated: EBS_Zambia_Report.html")