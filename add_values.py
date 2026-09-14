from dhis2 import Api, RequestException, \
    load_csv  # make sure you have dhis2.py installed, otherwise run "pip3 install dhis2.py"
import json
import logzero
from logzero import logger
from datetime import date, datetime, timedelta
import time
from collections import defaultdict
import pandas as pd
import numpy as np
import statistics
from re import match, findall, compile, search
from dhis2_env import api_from_env

api_source = api_from_env('DHIS2_TEST_REAL')

# setup the logger
log_file = "./get_datavalue_stats.log"
logzero.logfile(log_file)


def reindex(json_object, key):
    new_json = dict()
    for elem in json_object:
        key_value = elem[key]
        # elem.pop(key)
        new_json[key_value] = elem
    return new_json


def json_extract_nested_ids(obj, key):
    """
    Recursively fetch ids for a given key in a nested JSON

    Args:
      obj (list / dict): metadata object
      key (str): the key to find in the metadata object

    Returns:
        values (list) the ids found for the key in the object
    """
    arr = []

    def extract(obj, arr, key):
        """Recursively search for values of key in JSON tree."""
        if isinstance(obj, dict):
            for k, v in obj.items():
                # if the key is in the dictionary
                if k == key:
                    # the key points to a list eg, key is 'dataElements':
                    # "dataElements" : [
                    #    { "id": "UID1", "id": "UID2", ... }
                    # ]
                    if isinstance(v, list):
                        for item in v:
                            arr.append(item["id"]) if item["id"] not in arr else arr
                    # the key points to another dictionary eg, key is 'dataElement':
                    # "dataElement" : { "id": UID }
                    elif isinstance(v, dict):
                        arr.append(v["id"])
                    # if it is not a list or a dict, we simply take the value eg, key is organisationUnit
                    # "organisationUnit" : UID
                    else:
                        arr.append(v)
                # if key is not there but it is still a dict or a list,
                # call the extract function again to keep going down another level
                elif isinstance(v, (dict, list)):
                    extract(v, arr, key)
        # if it is a list, loop each element and call the extract function
        elif isinstance(obj, list):
            for item in obj:
                extract(item, arr, key)
        return arr

    values = extract(obj, arr, key)
    # Make sure the returned list contains no duplicate UIDs
    return list(dict.fromkeys(values))


def analyse_OU(ouLevels, OU, ouUID, ouValues, ouReporting, deUID):
    for level in sorted(ouLevels):
        if ouUID in OU[level]:
            ouValues[level][deUID] += 1
            add_reporting_ou_info(level, OU, ouUID, ouReporting, deUID)


def add_reporting_ou_info(level, OU, ouUID, ouReporting, deUID):
    if level != 1:
        if ouUID not in ouReporting[level][deUID]:
            ouReporting[level][deUID].append(ouUID)
            add_reporting_ou_info((level - 1), OU, OU[level][ouUID]['parent']['id'], ouReporting, deUID)
    else:
        return


def outliers_IQR(value_list):
    number_outliers = 0

    Q1 = np.percentile(value_list, 25)
    Q3 = np.percentile(value_list, 75)
    IQR = Q3 - Q1

    low_lim = Q1 - 1.5 * IQR
    up_lim = Q3 + 1.5 * IQR

    for val in value_list:
        if ((val > up_lim) or (val < low_lim)):
            number_outliers += 1

    return number_outliers


def main():
    # Open csv file
    current_csv_file = ''
    map = pd.read_csv('population_mapping.csv')
    map = map.fillna('')

    df_result = dict()
    df = dict()

    for dataSet_uid in list(map['source_dataSet_uid'].unique()):
        df_result[dataSet_uid] = pd.DataFrame()
        df[dataSet_uid] = pd.read_csv('datavalues_' + dataSet_uid + '.csv')
        df[dataSet_uid] = df[dataSet_uid][['dataElement', 'period', 'orgUnit', 'categoryOptionCombo', 'value']]


    time_aggregation = None
    #time_aggregation = 'MONTHLY_YEARLY'

    for index, row in map.iterrows():
        dataSet_uid = row['source_dataSet_uid']
        expression = row['source_DEs_UIDs']
        out_DE = row['target_DE_UID']
        catCombo = row['source_COC_UID']
        out_COC = row['target_COC_UID']
        # pattern = compile(r'#\{([a-zA-Z0-9]{11})\}')
        # z = pattern.findall(expression)
        # if z:
        DEs_UIDs = row['source_DEs_UIDs'].split(',')
        print(DEs_UIDs)
        result = df[dataSet_uid][df[dataSet_uid].dataElement.isin(DEs_UIDs)]

        if result.nunique()['dataElement'] == len(DEs_UIDs):
            logger.info('All dataElements in expression found')
        else:
            logger.error('DE missing!')
            exit(1)

        if time_aggregation == 'MONTHLY_YEARLY':
            # Remove the monthly part (last 2 chars). When we aggregate we get the total for the year
            result['period'] = result['period'].str[:-2]

        if catCombo != '' and catCombo != 'default':
            sum = result.groupby(["period", "orgUnit", "categoryOptionCombo"], as_index=False)["value"].sum()
            # Filter by the COCs
            COC_UIDs = catCombo.split(',')
            sum = sum[sum["categoryOptionCombo"].isin(COC_UIDs)]
        else:
            sum = result
        sum = sum.groupby(["period", "orgUnit"], as_index=False)["value"].sum()

        sum['dataelement'] = out_DE
        if out_COC != '':
            if '*' in out_COC:
                COC_list = out_COC.split('_')
                sum_copy = sum.copy()
                iteration = 1
                for factor_COC_UID in COC_list:
                    tmp = factor_COC_UID.split('*')
                    factor = float(tmp[0])
                    COC_UID = tmp[1]
                    if iteration == 1:
                        sum['categoryOptionCombo'] = COC_UID
                        sum['value'] = factor*sum['value']
                    else:
                        tmp = sum_copy.copy()
                        tmp['categoryOptionCombo'] = COC_UID
                        tmp['value'] = factor * tmp['value']
                        sum = pd.concat([sum, tmp])
                    iteration = iteration + 1
                if 'categoryOptionCombo' in sum:
                    sum = sum.rename(columns={"categoryOptionCombo": "categoryoptioncombo"})

            else:
                sum['categoryOptionCombo'] = out_COC
                sum = sum.rename(columns={"categoryOptionCombo": "categoryoptioncombo"})
        else:
            if 'categoryOptionCombo' in sum:
                #The column is present because we previously grouped by COC UID, but since out_COC
                #it means that we are mapping to default COC in target, so we can just drop the column
                sum.drop('categoryOptionCombo', inplace=True, axis=1)
                # Add the COC column empty
            sum['categoryoptioncombo'] = ''

        sum = sum.fillna('')

        sum = sum.rename(columns={"orgUnit": "orgunit"})

        # Round to highest integer
        sum['value'] = np.round(sum['value'])
        sum = sum.astype({"value": int})

        sum = sum.reindex(columns=['dataelement', 'categoryoptioncombo', 'orgunit', 'period', 'value'])

        if df_result[dataSet_uid].empty:
            df_result[dataSet_uid] = sum
        else:
            df_result[dataSet_uid] = pd.concat([df_result[dataSet_uid], sum])

        print(df_result[dataSet_uid].shape)

    for dataSet_uid in list(map['source_dataSet_uid'].unique()):
        # Last aggregation
        df_result[dataSet_uid] = df_result[dataSet_uid].groupby(['dataelement', 'categoryoptioncombo', 'orgunit', 'period'], as_index=False)["value"].sum()
        df_result[dataSet_uid] = df_result[dataSet_uid].rename(columns={"categoryoptioncombo": "catoptcombo"})
        export_csv = df_result[dataSet_uid].to_csv(r'./dataValueSets_' + dataSet_uid + '.csv', index=None, header=True)






if __name__ == '__main__':
    main()
