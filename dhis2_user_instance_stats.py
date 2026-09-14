import csv
from dhis2 import Api, RequestException
import time

# File paths
CSV_FILE = "users_and_instances.csv"
PASSWORDS_FILE = "generated_passwords.csv"
OUTPUT_FILE = "metadata_summary.csv"

# Read username-password mapping
user_passwords = {}
with open(PASSWORDS_FILE, newline='') as pw_file:
    reader = csv.reader(pw_file)
    next(reader)  # skip header
    for row in reader:
        username, password = row
        user_passwords[username.strip()] = password.strip()

# Read user-instance mapping
user_instance_data = []
with open(CSV_FILE, newline='') as csvfile:
    reader = csv.reader(csvfile)
    for row in reader:
        username, instance_url = row
        username = username.strip()
        instance_url = instance_url.strip().replace("/web-login/", "")
        if username in user_passwords:
            user_instance_data.append((username, user_passwords[username], instance_url))

# Prepare to collect metadata counts
summary_data = []

# Metadata types (excluding 'dataElements' since we handle it manually)
metadata_types = [
    "categoryOptions", "categories", "categoryCombos",
    "dataSets", "sections", "validationRules", "indicators", "optionSets",
    "trackedEntityAttributes", "trackedEntityTypes", "programs", "programSections", "programStages",
    "programStageSections", "programRuleVariables", "programRules", "programRuleActions", "programIndicators"
]

# Query and collect metadata counts per user-instance
for username, password, instance_url in user_instance_data:
    start = time.time()

    try:
        api = Api(instance_url, username, password)
        params = {"paging": "false"}

        metadata_total = {}

        # Get domain-specific dataElement counts
        data_elements = api.get("dataElements", params={"paging": "false", "fields":"id,name,domainType"}).json()["dataElements"]
        agg_count = sum(1 for de in data_elements if de.get("domainType") == "AGGREGATE")
        trk_count = sum(1 for de in data_elements if de.get("domainType") == "TRACKER")
        metadata_total["dataElements_AGG"] = agg_count
        metadata_total["dataElements_TRK"] = trk_count

        # Get all other metadata counts
        for key in metadata_types:
            metadata_total[key] = len(api.get(key, params=params).json().get(key, []))

        row = [username, instance_url] + [metadata_total.get(key, 0) for key in ["dataElements_AGG", "dataElements_TRK"] + metadata_types]
        summary_data.append(row)

    except RequestException:
        summary_data.append([username, instance_url] + ["ERROR"] * (2 + len(metadata_types)))

    end = time.time()
    print(f"Elapsed time for instance {instance_url} and user {username}: {end - start:.2f}s")

# Write output to CSV
with open(OUTPUT_FILE, mode='w', newline='') as out_file:
    writer = csv.writer(out_file)
    writer.writerow(["username", "instance_url", "dataElements_AGG", "dataElements_TRK"] + metadata_types)
    writer.writerows(summary_data)

print("Metadata summary saved to:", OUTPUT_FILE)
