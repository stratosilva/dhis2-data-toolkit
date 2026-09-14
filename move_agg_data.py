from dhis2 import Api, RequestException, setup_logger, logger, \
    is_valid_uid  # make sure you have dhis2.py installed, otherwise run "pip3 install dhis2.py"
import json
from tools.dhis2 import post_chunked_data
from dhis2_env import api_from_env

instance_source = 'https://metadata.dev.dhis2.org/campaign'
instance_target = 'https://demos.dhis2.org/hmis_dev'

# Credentials come from .env (see .env.example), not from auth.json.
api_source = api_from_env('DHIS2_CAMPAIGN', url=instance_source)
api_target = api_from_env('DHIS2_HMIS_DEV', url=instance_target)

setup_logger()

startDate = '2022-01-01'
endDate = '2023-12-31'

def main():
    logger.warning("Server source running DHIS2 version {} revision {}".format(api_source.version, api_source.revision))
    logger.warning("Server target running DHIS2 version {} revision {}".format(api_target.version, api_target.revision))

    # Get org units level 4
    ous = api_source.get('organisationUnits', params={"filter": "level:eq:1", "paging": "false", "fields":"id,name"}).json()[
        'organisationUnits']
    datasets = api_source.get('dataSets', params={"filter": "name:like:SIA", "paging": "false", "fields":"id,name"}).json()[
        'dataSets']
    c = True
    for ds in datasets:
        for ou in ous:
            logger.info('Processing data for dataSet ' + ds['name'] + ' from ' + ou['name'])
            params = {
                #'period': row['period'],
                'startDate': startDate,
                'endDate': endDate,
                'orgUnit': ou['id'],
                'dataSet': ds['id'],
                'children': 'true'
            }

            # Get data from source server
            data = api_source.get('dataValueSets', params=params).json()
            if 'dataValues' in data and len(data['dataValues']) > 0:
                post_chunked_data(api_target, data['dataValues'], 'dataValueSets', 1000)
            else:
                logger.info("No data for '{}' '{}' ".format(ou['name'], ds['name']))


if __name__ == '__main__':
    main()
