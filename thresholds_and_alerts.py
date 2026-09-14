from dhis2 import Api
import pandas as pd
import time
from datetime import datetime
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dhis2_env import api_from_env, env

# Global variables
rootOU = 'xxxx' # Root Org Unit UID, normally the country
dataSet = 'yyyy' # UID of IDSR dataSet
api = api_from_env('ALERTS_DHIS2') # To be able to access the DHIS2 server API
email_to_alert = { "District UID" : ["email1@gmail.com", "email2@gmail.com"]}

# SMTP server details (see .env)
smtp_server = env('ALERTS_SMTP_SERVER', 'smtp.gmail.com')
port = int(env('ALERTS_SMTP_PORT', '465'))
sender_email = env('ALERTS_SMTP_SENDER', required=True)
password = env('ALERTS_SMTP_PASSWORD', required=True)


# Function to send email alert
def send_email_alert(smtp_server, port, sender_email, password, recipient_emails, subject, body):
    # Create the email message
    msg = MIMEMultipart()
    msg['From'] = sender_email
    msg['To'] = ', '.join(recipient_emails)
    msg['Subject'] = subject

    # Add the email body
    msg.attach(MIMEText(body, 'plain'))

    try:
        # Connect to the SMTP server and send the email
        with smtplib.SMTP_SSL(smtp_server, port) as server:
            server.login(sender_email, password)
            server.sendmail(sender_email, recipient_emails, msg.as_string())
        print(f"Email sent successfully to {', '.join(recipient_emails)}")
    except Exception as e:
        print(f"Failed to send email to {', '.join(recipient_emails)}. Error: {e}")


# Function to fetch aggregated data from DHIS2 analytics endpoint
def fetch_aggregated_data(api, dx, pe, ou):
    # Construct the dimensions parameter
    dx_dimension = f"dx:{','.join(dx)}" # List of DEs - Surveillance diseases
    pe_dimension = f"pe:{','.join(pe)}" # One week, multiple weeks
    ou_dimension = f"ou:{ou}" # Org unit or org unit level - District level?

    # Set up the parameters for the API call
    params = {
        'dimension': f"{dx_dimension};{pe_dimension};{ou_dimension}",
        'displayProperty': 'NAME',
        'includeNumDen': 'true',
        'skipMeta': 'true',
        'skipData': 'false'
    }

    retries = 0
    while retries <= 5:
        try:
            # API call to analytics endpoint
            response = api.get('analytics', params=params)
            response.raise_for_status()
            data = response.json()
            break
        except Exception as e:
            print(f"Error fetching analytics data: {e}")
            time.sleep(1)
            retries += 1
            data = None

    if not data:
        return None

    # Convert the response 'rows' to a DataFrame
    headers = [header['name'] for header in data.get('headers', [])]
    rows = data.get('rows', [])
    df = pd.DataFrame(rows, columns=headers)

    # Rename the relevant columns
    df.rename(columns={'dx': 'dataElement', 'ou': 'orgUnit'}, inplace=True)

    # Keep only the first 4 columns: dataElement, pe, orgUnit, value
    df = df[['dataElement', 'pe', 'orgUnit', 'value']]

    # df should be a table with columns: dataElement (UID), orgUnit (UID), pe (ISO format), value
    return df


# Function to fetch data from DHIS2 using dataValueSets (raw data entered, no aggregation)
# Use period / date range + dataset / data element)
def fetch_data_from_dhis2(api, rootOU, dsUID=None, deUID=None, pe=None, startDate=None, endDate=None):
    # Ensure either dataSet or dataElement UID is provided
    if not dsUID and not deUID:
        raise ValueError("Either 'dsUID' (dataSet) or 'deUID' (dataElement) must be provided.")

    # Create the parameters dictionary based on input
    params = {
        'orgUnit': rootOU,
        'children': 'true'
    }

    # Use period and dataSet when pulling values for current week to check against threshold
    # Use startDate, endDate and dataElement to pull historical values which will be used to calculate a threshold

    # Use either 'period' or 'startDate'/'endDate' depending on the inputs
    if pe:
        params['period'] = pe
    elif startDate and endDate:
        params['startDate'] = startDate
        params['endDate'] = endDate
    else:
        raise ValueError("Either 'pe' or both 'startDate' and 'endDate' must be provided.")

    # Include the dataSet or dataElement based on what was provided
    if dsUID:
        params['dataSet'] = dsUID
    elif deUID:
        params['dataElement'] = deUID

    retries = 0
    while retries <= 5:
        try:
            response = api.get('dataValueSets', params=params)
            response.raise_for_status()  # Raise an exception for HTTP errors
            data = response.json()
            break
        except Exception as e:
            print(f"Error fetching data: {e}")
            time.sleep(1)
            retries += 1
            data = None

    if not data:
        return None

    # Process JSON to a DataFrame
    data_values = data.get("dataValues", [])
    df = pd.DataFrame(data_values)

    # df should be a table with columns: dataElement (UID), orgUnit (UID) will be facility, pe (ISO format), value
    return df


# Replace UIDs with names using API lookup
def replace_uids_with_names(api, df):
    # Get unique UIDs for each column
    de_uids = df['dataElement'].unique().tolist()
    ou_uids = df['orgUnit'].unique().tolist()
    coc_uids = df['categoryOptionCombo'].unique().tolist()

    # Fetch names for dataElements, organisationUnits, categoryOptionCombos
    des = get_names(api, 'dataElements', de_uids)
    ous = get_names(api, 'organisationUnits', ou_uids)
    cocs = get_names(api, 'categoryOptionCombos', coc_uids)

    # Replace UIDs with names in the dataframe
    df['dataElement'] = df['dataElement'].map(des)
    df['orgUnit'] = df['orgUnit'].map(ous)
    df['categoryOptionCombo'] = df['categoryOptionCombo'].map(cocs)

    return df


# Helper function to get names from API
def get_names(api, endpoint, uids):
    params = {
        'filter': f"id:in:[{','.join(uids)}]",
        'fields': 'id,name'
    }
    response = api.get(endpoint, params=params).json()
    items = response.get(endpoint, [])
    id_name_mapping = {item['id']: item['name'] for item in items}

    return id_name_mapping


# Get the current ISO week in the required format (e.g., 2024W1)
def get_current_iso_week():
    current_date = datetime.now()
    iso_year, iso_week, _ = current_date.isocalendar()
    return f"{iso_year}W{iso_week}"


# Example of main flow
def main():
    # Get current values for this week to see if anywhere we go over threshold
    current_week = get_current_iso_week()
    df = fetch_data_from_dhis2(api, current_week, rootOU, dataSet)
    # Convert value to numeric in case it is returned as a string
    df['value'] = pd.to_numeric(df['value'], errors='coerce')

    # An example for Measles. Let's imagine that the uid for Measles is 'xyz'
    # we could get all the facilities this week which have one or more cases of Measles
    measles_uid = 'xyz'
    measles_df = df[(df['dataElement'] == measles_uid) & (df['value'] >= 1)]
    if not measles_df.empty:
        df_with_names = replace_uids_with_names(api, measles_df)
        print("We need to alert the following facilities for Measles cases")
        print(df_with_names)
    else:
        print("No measles cases with value >= 1")

    # An example to analyze Measles cases at district level
    # Example UIDs
    dx_list = ['yMlASPYVU2v', 'K2Ky3gBe2sB']  # Replace with actual data element UIDs
    periods = ['THIS_WEEK', 'LAST_12_WEEKS']  # Example of relative periods
    district_level_uid = 'LEVEL-b5jE033nBqM'  # UID for the district level
    df_analytics = fetch_aggregated_data(api, dx=[measles_uid], pe=['THIS_WEEK'], ou=district_level_uid)
    df_analytics['value'] = pd.to_numeric(df_analytics['value'], errors='coerce')

    measles_df = df_analytics[df_analytics['value'] >= 1]
    if not measles_df.empty:
        df_with_names = replace_uids_with_names(api, measles_df)
        print("We need to alert the following districts for Measles cases")
        print(df_with_names)
    else:
        print("No measles cases with value >= 1")

    # Alert people in district
    # Replace UIDs with actual district names for 'orgUnit' column - it will be handy later on to build the message
    org_unit_uids = df_analytics['orgUnit'].unique().tolist()
    org_unit_names = get_names(api, 'organisationUnits', org_unit_uids)
    # Iterate over each district where Measles cases (value >= 1) was detected
    for index, row in df_analytics.iterrows():
        district_uid = row['orgUnit']
        value = row['value']

        district_name = org_unit_names.get(district_uid, 'Unknown District')
        # Check if the district UID is in the email_to_alert dictionary
        if district_uid in email_to_alert:
            recipient_emails = email_to_alert[district_uid]

            # Create the email subject and body
            subject = f"Alert: {value} cases of Measles detected in {district_name}"
            body = f"{value} cases of Measles detected in district {district_name}.\n\nPlease take the necessary actions."

            # Send the email
            send_email_alert(
                smtp_server=smtp_server,
                port=port,
                sender_email=sender_email,
                password=password,
                recipient_emails=recipient_emails,
                subject=subject,
                body=body
            )

    # If we need to have both pieces of information, facility and district, for example if we need to notify
    # the district supervisors but adding information on the facilities which went over the threshold
    # the best way is to use a table in csv with district uid - facility uid, or just run a sql view and get the
    # result
    # This query is for org unit levels as follows: Facility (5), Block (4), District (3)
    #select facility.uid as facility_uid, facility.name as facility_name, district.uid as district_uid, district.name as district_name
    #from organisationunit facility
    #inner join organisationunit block on facility.parentid = block.organisationunitid
    #inner join organisationunit district on block.parentid = district.organisationunitid;









