import csv
import random
import string
from collections import defaultdict
import json
from dhis2 import Api, RequestException
from dhis2_env import api_from_env, env

# File paths
CSV_FILE = "users_and_instances.csv"
OUS_FILE = "westland_ous.json"
ROLES_FILE = "metadata_user_roles.json"
OUTPUT_PASSWORDS_FILE = "generated_passwords.csv"

# Constants
SUPERUSER_ROLE_ID = "yrB6vc5Ip3r"
ORG_UNIT_ID = "WUaXUz8cN2C"  # Assumed single OU for all
USER_ROLES = {
    "Admin": "mUyeAxU4veM",
    "Analytics": "J7SKbb9MEoQ",
    "Data Capture": "n39tkBLrVG7"
}
DEFAULT_ADMIN_USER = env('DHIS2_DEFAULT_ADMIN_USERNAME')
DEFAULT_ADMIN_PASS = env('DHIS2_DEFAULT_ADMIN_PASSWORD')

# Load JSON payloads
with open(OUS_FILE, encoding='utf-8') as f:
    ous_payload = json.load(f)

with open(ROLES_FILE, encoding='utf-8') as f:
    roles_payload = json.load(f)

# Read CSV and organize by instance
instance_users = defaultdict(list)
with open(CSV_FILE, newline='') as csvfile:
    reader = csv.reader(csvfile)
    for row in reader:
        username, instance_url = row
        instance_url = instance_url.strip().replace("/web-login/", "")
        instance_users[instance_url].append(username.strip())

# Generate secure random password
def generate_password():
    specials = "!@#$%^&*"
    password = [
        random.choice(string.ascii_uppercase),
        random.choice(string.digits),
        random.choice(specials),
    ]
    password += random.choices(string.ascii_letters + string.digits + specials, k=5)
    random.shuffle(password)
    return ''.join(password)

# Store generated passwords
generated_passwords = []

# Process each instance
for instance_url, users in instance_users.items():
    try:
        # api = Api(instance_url, DEFAULT_ADMIN_USER, DEFAULT_ADMIN_PASS)
        #
        # # POST Org Units
        # api.post("metadata", ous_payload)
        #
        api = api_from_env('DHIS2_ROBOT', url=instance_url)
        # POST User Roles
        api.post("metadata", roles_payload)

        # Create 'robot' user
        # robot_payload = {
        #     "firstName": "Admin",
        #     "surname": "Robot",
        #     "password": env('DHIS2_ROBOT_PASSWORD'),
        #     "username": "robot",
        #     "organisationUnits": [{"id": ORG_UNIT_ID}],
        #     "dataViewOrganisationUnits": [{"id": ORG_UNIT_ID}],
        #     "teiSearchOrganisationUnits": [{"id": ORG_UNIT_ID}],
        #     "userRoles": [{"id": SUPERUSER_ROLE_ID}]
        # }
        # api.post("users", robot_payload)

        # Create users
        # for username in users:
        #     password = generate_password()
        #     payload = {
        #         "firstName": "Student",
        #         "surname": username.capitalize(),
        #         "password": password,
        #         "username": username,
        #         "userRoles": [
        #             {"id": USER_ROLES["Admin"]},
        #             {"id": USER_ROLES["Analytics"]},
        #             {"id": USER_ROLES["Data Capture"]}
        #         ],
        #         "organisationUnits": [{"id": ORG_UNIT_ID}],
        #         "dataViewOrganisationUnits": [{"id": ORG_UNIT_ID}],
        #         "teiSearchOrganisationUnits": [{"id": ORG_UNIT_ID}]
        #     }
        #     api.post("users", payload)
        #     generated_passwords.append((username, password))

        # Disable the user account by ID
        # admin_user_id = "M5zQapPyTZI"
        # payload = {
        #     "id": admin_user_id,
        #     "firstName": "admin",
        #     "surname": "admin",
        #     "userRoles": [
        #         {"id": "yrB6vc5Ip3r"}  # Superuser role
        #     ],
        #     "username": "admin",
        #     "disabled": True,
        #     "organisationUnits": [],
        #     "dataViewOrganisationUnits": [],
        #     "teiSearchOrganisationUnits": []
        # }
        # api = api_from_env('DHIS2_ROBOT', url=instance_url)
        # api.put(f"users/{admin_user_id}", payload)

    except RequestException as e:
        print(f"Failed for {instance_url}: {e}")


# Write passwords to CSV
# with open(OUTPUT_PASSWORDS_FILE, mode='w', newline='') as file:
#     writer = csv.writer(file)
#     writer.writerow(["Username", "Password"])
#     writer.writerows(generated_passwords)

print("Finished creating users and saving passwords.")
