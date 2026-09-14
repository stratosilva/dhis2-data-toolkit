from dhis2 import Api, RequestException, setup_logger
import json
import random
from faker import Faker
from datetime import datetime
import logging

logging.basicConfig(
    filename='./add_diabetes.log',
    filemode='w',  # Overwrites the log file each time the script runs
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger()

# Initialize Faker
fake = Faker()

from tools.dhis2 import post_chunked_data
from dhis2_env import api_from_env

api = api_from_env('ECUADOR_DEMO')

def reindex(json_object, key):
	new_json = dict()
	for elem in json_object:
		key_value = elem[key]
		# elem.pop(key)
		new_json[key_value] = elem
	return new_json

def json_extract(obj, key):
    """Recursively fetch values from nested JSON."""
    arr = []

    def extract(obj, arr, key):
        """Recursively search for values of key in JSON tree."""
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(v, (dict, list)):
                    extract(v, arr, key)
                elif k == key:
                    arr.append(v)
        elif isinstance(obj, list):
            for item in obj:
                extract(item, arr, key)
        return arr

    values = extract(obj, arr, key)
    return values


def main():
    logger.warning("Server source running DHIS2 version {} revision {}".format(api.version, api.revision))

    # Get facilities in the demo instance
    ous_westland = \
    api.get('organisationUnits', params={"filter": "level:eq:5", "paging": "false", "fields": "id,name"}).json()[
        'organisationUnits']
    ous_westland = reindex(ous_westland, 'id')
    facility_uids = list(ous_westland.keys())

    # Loop through each facility
    for facility_uid in facility_uids:
        teis_ready_to_post = []
        # Determine the type of blood sugar test available for the facility
        diabetes_type = random.choice(["FBS", "HBA1C"])
        if diabetes_type != "HBA1C":
            blood_sugar_unit = random.choice(["MG_OR_DL", "MMOL_OR_L"])

        # Get enrollments for the facility
        enrollments = api.get('tracker/enrollments',
                              params={"orgUnit": facility_uid, "skipPaging": "true",
                                      "fields": "trackedEntity"}).json().get('instances', [])
        # Determine the proportion of controlled vs uncontrolled diabetic patients
        uncontrolled_proportion = random.uniform(0.3, 0.8)
        logger.info('Processing org unit ' + ous_westland[facility_uid]['name'] + " with proportion uncontrolled:" + str(uncontrolled_proportion))

        tei_uids = json_extract(enrollments, 'trackedEntity')

        # Loop through each tei
        for tei_uid in tei_uids:
            has_diabetes = random.random() < uncontrolled_proportion

            # Get the full details of the tracked entity instance
            tei = api.get('trackedEntityInstances/' + tei_uid,
                                 params={"program": 'pMIglSEqPGS', "fields": "*"}).json()

            if 'attributes' in tei['enrollments'][0]:
                tei['enrollments'][0].pop('attributes')

            # Loop through each enrollment and event
            for enrollment in tei.get('enrollments', []):
                for event in enrollment.get('events', []):
                    # Modify specific dataValues
                    data_values_to_keep = []
                    for dv in event.get('dataValues', []):
                        if dv['dataElement'] not in ['Xjrw5pbxBJw', 'cE3KOFmoYbZ', 'oOHbZP28H2l', 'v7nC8vSTzwh', 'JZlJfx3oihw']:
                            data_values_to_keep.append(dv)
                    event['dataValues'] = data_values_to_keep

            # Set diabetes attribute to 'YES'
            found = False
            for attr in tei['attributes']:
                if attr['attribute'] == 'v4DnYfXn9Mu':
                    attr['value'] = 'YES'
                    found = True
                    break
            if not found:
                tei['attributes'].append({
                    "attribute": "v4DnYfXn9Mu",
                    "value": 'YES',
                    "displayName": "HTN - Does patient have diabetes?",
                    "valueType": "TEXT"
                })

            if has_diabetes:
                # Loop through each event to set values for uncontrolled hypertension
                for enrollment in tei.get('enrollments', []):
                    for event in enrollment.get('events', []):
                        if event['status'] not in ['ACTIVE', 'COMPLETED']:
                            continue
                        data_values_to_add = []
                        if diabetes_type == "FBS":
                            if blood_sugar_unit == "MG_OR_DL":
                                blood_sugar_value = random.randint(126, 230)
                                data_element_id = "oOHbZP28H2l"
                            else:
                                blood_sugar_value = round(random.uniform(7, 12), 1)
                                data_element_id = "v7nC8vSTzwh"
                        elif diabetes_type == "HBA1C":
                            blood_sugar_value = round(random.uniform(7, 10), 1)
                            data_element_id = "JZlJfx3oihw"
                        if diabetes_type != "HBA1C":
                            data_values_to_add.append({
                                "dataElement": "Xjrw5pbxBJw",
                                "value": blood_sugar_unit
                            })
                        data_values_to_add.append({
                            "dataElement": "cE3KOFmoYbZ",
                            "value": diabetes_type
                        })
                        data_values_to_add.append({
                            "dataElement": data_element_id,
                            "value": str(blood_sugar_value)
                        })
                        event['dataValues'].extend(data_values_to_add)
            else:
                tei_has_multiple_events = False
                first_event_set = False

                # Check if TEI has more than one event
                for enrollment in tei.get('enrollments', []):
                    if len(enrollment.get('events', [])) > 1:
                        tei_has_multiple_events = True
                        break

                first_event = True
                if tei_has_multiple_events:
                    # Loop through events and set values for the first event
                    for i, event in enumerate(enrollment.get('events', [])):
                        if event['status'] not in ['ACTIVE', 'COMPLETED']:
                            continue
                        data_values_to_add = []
                        if diabetes_type == "FBS":
                            if blood_sugar_unit == "MG_OR_DL":
                                blood_sugar_value = random.randint(126, 230) if first_event else random.randint(70, 125)
                                data_element_id = "oOHbZP28H2l"
                            else:
                                blood_sugar_value = round(random.uniform(7, 12), 1) if first_event else round(
                                    random.uniform(3.9, 6.9), 1)
                                data_element_id = "v7nC8vSTzwh"
                        elif diabetes_type == "HBA1C":
                            blood_sugar_value = round(random.uniform(7, 10), 1) if first_event else round(
                                random.uniform(5.6, 6.9), 1)
                            data_element_id = "JZlJfx3oihw"

                        if diabetes_type != "HBA1C":
                            data_values_to_add.append({
                                "dataElement": "Xjrw5pbxBJw",
                                "value": blood_sugar_unit
                            })
                        data_values_to_add.append({
                            "dataElement": "cE3KOFmoYbZ",
                            "value": diabetes_type
                        })
                        data_values_to_add.append({
                            "dataElement": data_element_id,
                            "value": str(blood_sugar_value)
                        })
                        event['dataValues'].extend(data_values_to_add)

                        first_event = False

                else:
                    # For only one event a patient with no diabetes will be recorded as NO
                    found = False
                    for attr in tei['attributes']:
                        if attr['attribute'] == 'v4DnYfXn9Mu':
                            attr['value'] = 'NO'
                            found = True
                            break
                    # Set values for single event TEIs with no diabetes
                    for enrollment in tei.get('enrollments', []):
                        for event in enrollment.get('events', []):
                            if event['status'] not in ['ACTIVE', 'COMPLETED']:
                                continue
                            data_values_to_add = []
                            if diabetes_type == "FBS":
                                if blood_sugar_unit == "MG_OR_DL":
                                    blood_sugar_value = random.randint(70, 125)
                                    data_element_id = "oOHbZP28H2l"
                                else:
                                    blood_sugar_value = round(random.uniform(3.9, 6.9), 1)
                                    data_element_id = "v7nC8vSTzwh"
                            elif diabetes_type == "HBA1C":
                                blood_sugar_value = round(random.uniform(5.6, 6.9), 1)
                                data_element_id = "JZlJfx3oihw"

                            data_values_to_add.append({
                                "dataElement": "Xjrw5pbxBJw",
                                "value": blood_sugar_unit
                            })
                            data_values_to_add.append({
                                "dataElement": "cE3KOFmoYbZ",
                                "value": diabetes_type
                            })
                            data_values_to_add.append({
                                "dataElement": data_element_id,
                                "value": str(blood_sugar_value)
                            })
                            event['dataValues'].extend(data_values_to_add)

                    # Set diabetes attribute to 'NO'
                    found = False
                    for attr in tei['attributes']:
                        if attr['attribute'] == 'v4DnYfXn9Mu':
                            attr['value'] = 'NO'
                            found = True
                            break
                        if not found:
                                tei['attributes'].append({
                                    "attribute": "v4DnYfXn9Mu",
                                    "value": 'NO',
                                    "displayName": "HTN - Does patient have diabetes?",
                                    "valueType": "TEXT"
                                })

            teis_ready_to_post.append(tei)

        if len(teis_ready_to_post) > 0:
            logger.info('Posting TEIs to org unit ' + ous_westland[facility_uid]['name'])
            post_chunked_data(api, teis_ready_to_post, 'trackedEntityInstances', 50)


if __name__ == '__main__':
    main()
