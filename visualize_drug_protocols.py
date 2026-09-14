import pandas as pd
import matplotlib.pyplot as plt

data = pd.read_csv('puc_meds_nigeria.csv')

# Output unique values for each column
print("Unique values before cleaning:")
for column in ['amlodipine', 'losartan', 'telmisartan', 'hydrochlorothiazide']:
    print(f"{column}: {data[column].dropna().unique()}")

# Define cleaning functions
def clean_amlodipine(value):
    return value.replace('_OD', '') if isinstance(value, str) else value

def clean_losartan(value):
    return value.replace('_OD', '') if isinstance(value, str) else value

def clean_telmisartan(value):
    return value.replace('_OD', '') if isinstance(value, str) else value

def clean_hydrochlorothiazide(value):
    if isinstance(value, str):
        value = value.replace('_OD', '').replace('HYDROCHLORATHIAZIDE', 'HYDROCHLOROTHIAZIDE')
    return value

# Apply cleaning functions to the relevant columns
data['amlodipine'] = data['amlodipine'].apply(clean_amlodipine)
data['losartan'] = data['losartan'].apply(clean_losartan)
data['telmisartan'] = data['telmisartan'].apply(clean_telmisartan)
data['hydrochlorothiazide'] = data['hydrochlorothiazide'].apply(clean_hydrochlorothiazide)

# Drop duplicates based on facility_name, last_visit_date, and patient_id, keeping rows with non-empty medication values
def has_medication_values(row):
    return any(pd.notna(row[col]) for col in ['amlodipine', 'losartan', 'telmisartan', 'hydrochlorothiazide', 'other'])

data = data.sort_values(by=['amlodipine', 'losartan', 'hydrochlorothiazide', 'telmisartan'], na_position='last')
data = data.loc[data.duplicated(subset=['facility_name', 'last_visit_date', 'patient_id'], keep=False) & data.apply(has_medication_values, axis=1) |
                ~data.duplicated(subset=['facility_name', 'last_visit_date', 'patient_id'], keep=False)]

# Output unique values after cleaning
print("\nUnique values after cleaning:")
for column in ['amlodipine', 'losartan', 'telmisartan', 'hydrochlorothiazide', 'other']:
    print(f"{column}: {data[column].dropna().unique()}")

# Define protocol phases
protocol_stages = {
    1: ['AMLODIPINE_5MG'],
    2: ['AMLODIPINE_5MG', 'LOSARTAN_50MG'],
    3: ['AMLODIPINE_10MG', 'LOSARTAN_100MG'],
    4: ['LOSARTAN_100MG', 'HYDROCHLOROTHIAZIDE_25MG']
}

# Abbreviations for custom combinations
abbreviations = {
    'AMLODIPINE': 'AMLO',
    'LOSARTAN': 'LOS',
    'TELMISARTAN': 'TELMI',
    'HYDROCHLOROTHIAZIDE': 'HCTZ'
}

def determine_protocol(row):
    meds = []
    if pd.notna(row['amlodipine']):
        meds.append(row['amlodipine'])
    if pd.notna(row['losartan']):
        meds.append(row['losartan'])
    if pd.notna(row['telmisartan']):
        meds.append(row['telmisartan'])
    if pd.notna(row['hydrochlorothiazide']):
        meds.append(row['hydrochlorothiazide'])

    # Check against protocol phases
    for phase, protocol_meds in protocol_stages.items():
        if sorted(meds) == sorted(protocol_meds):
            return f"Phase {phase}"

    # If no treatment but 'Other' has a value
    if not meds:
        if pd.notna(row['other']):
            return "Other"
        else:
            return "No treatment"

    # Create custom combination if not in protocol
    custom_combo = "+".join(
        abbreviations[med.split('_')[0]] + med.split('_')[1] for med in meds
    )

    return custom_combo

# Add Protocol column
data['Protocol'] = data.apply(determine_protocol, axis=1)

# Group by protocol and generate bar chart
protocol_counts = data['Protocol'].value_counts()
print("\nProtocol counts:")
print(protocol_counts)

# Plot bar chart
protocol_counts.plot(kind='bar', title='Protocol Distribution', xlabel='Protocol', ylabel='Number of Patients', figsize=(10, 6))


# Save processed data to a new CSV file
output_path = 'processed_data.csv'
data.to_csv(output_path, index=False)
print(f"Processed data saved to {output_path}")
