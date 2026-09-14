from dhis2 import Api, generate_uid, RequestException, setup_logger, logger
from io import StringIO
import pandas as pd
import requests as rq
import sys
import time
import re
import json
from dhis2_env import credentials

dhis_url, dhis_uname, dhis_pwd = credentials('UGANDA_HMIS')
api = Api(dhis_url, dhis_uname, dhis_pwd)

sql_view_uid = 'Kade4AwhihA'

mapping_config_csv_file = 'DEs_mapping.csv'

setup_logger(logfile='cumulative.log')


def update_period_in_SQL_view(sql_view, startDate, endDate) :
    sql_query = sql_view['sqlQuery']
    sql_view_uid = sql_view['id']
    sql_query = re.sub(r"startdate\s*>=\s*'([0-9]{4}-[0-9]{2}-[0-9]{2})", "startdate >= '"+startDate, sql_query)
    sql_query = re.sub(r"(enddate\s*<=\s*')([0-9]{4}-[0-9]{2}-[0-9]{2})", "enddate <= '"+endDate, sql_query)
    sql_view['sqlQuery'] = sql_query
    try:
        response = api.put('sqlViews/' + sql_view_uid, params={'mergeMode': 'REPLACE'}, json=sql_view)
    except RequestException as e:
        logger.error(e)
    else :
        logger.info('SQL View ' + sql_view_uid + ' - Period succcessfully updated')


def update_start_year_in_SQL_view(sql_view, year) :
    sql_query = sql_view['sqlQuery']
    sql_view_uid = sql_view['id']
    sql_query = re.sub(r"startdate\s*>=\s*'([0-9]{4}-[0-9]{2}-[0-9]{2})", "startdate >= '"+str(year)+"-01-01", sql_query)
    sql_view['sqlQuery'] = sql_query
    try:
        response = api.put('sqlViews/' + sql_view_uid, params={'mergeMode': 'REPLACE'}, json=sql_view)
    except RequestException as e:
        logger.error(e)
    else :
        logger.info('SQL View ' + sql_view_uid + ' - Period succcessfully updated')

def update_DE_UID_in_SQL_view(sql_view, de_UID) :
    sql_query = sql_view['sqlQuery']
    sql_view_uid = sql_view['id']
    sql_query = re.sub(r"WHERE\s*de.uid\s*=\s*'([0-9A-Za-z]{11})", "WHERE de.uid = '"+de_UID, sql_query)
    sql_view['sqlQuery'] = sql_query
    try:
        response = api.put('sqlViews/' + sql_view_uid, params={'mergeMode': 'REPLACE'}, json=sql_view)
    except RequestException as e:
        logger.error(e)
    else :
        logger.info('SQL View ' + sql_view_uid + ' - DE succcessfully updated')


def get_csv_from_SQL_view(sql_view) :
    sql_query = sql_view['sqlQuery']
    sql_view_uid = sql_view['id']
    try:
        result = api.post('sqlViews/' + sql_view_uid + '/execute', params={}, json={})
    except RequestException as e:
        logger.error(e)
    else :
        logger.info('SQL View executed succcessfully')
        if 'httpStatus' in result and result['httpStatus'] == 'OK' :
            print(result['message'], end="", flush=True)

    response=rq.get(dhis_url + '/api/sqlViews/' + sql_view_uid + '/data.csv', auth=(dhis_uname,dhis_pwd))
    #Read the data to a pandas dataset
    return pd.DataFrame(pd.read_csv(StringIO(response.text)))


def post_to_server(api, jsonObject, apiObject='metadata', strategy='CREATE_AND_UPDATE'):
    try:
        response = api.post(apiObject, params={'mergeMode': 'REPLACE', 'importStrategy': strategy},
                            json=jsonObject)

    except RequestException as e:
        # Print errors returned from DHIS2
        logger.error("metadata update failed with error " + str(e))
        pass
    else:
        if response is None:
            logger.error("Error in response from server")
            return
        text = json.loads(response.text)
        # print(text)
        if text['status'] == 'ERROR':
            logger.error("Import failed!!!!\n" + json.dumps(text['typeReports'], indent=4, sort_keys=True))
            with open('post_error.json', 'w') as f:
                json.dump(text['typeReports'], f, indent=4, sort_keys=True)
            return False
        # errorCode = errorReport['errorCode']
        else:
            if apiObject == 'metadata':
                logger.info("metadata imported " + text['status'] + " " + json.dumps(text['stats']))
            else:
                # logger.info("data imported " + text['status'] + " " + json.dumps(text['importCount']))
                logger.info("Data imported\n" + json.dumps(text, indent=4, sort_keys=True))
                with open('post_error.json', 'w') as f:
                    json.dump(text, f, indent=4, sort_keys=True)
                if text['status'] == 'WARNING': logger.warning(text)
            return True


def get_datavaluesets_from_column(df, column_name, de_uid) :

    df = df[['orgUnit', 'period', column_name]]
    df.rename(columns={column_name : 'value'}, inplace=True)
    df = df.dropna()
    df = df.astype({"value": int })
    df['dataElement'] = de_uid
    dataValueSets = { "dataValues" : df.to_dict(orient='records')}
    print(df)
    return dataValueSets


##########################################################################################################################

if len(sys.argv) > 1:
    sys.argv[1] == '-current_year'
    from datetime import date
    year = date.today().year
else:
    year = '2019'


df_with_DEs = pd.read_csv(mapping_config_csv_file)

for index, row in df_with_DEs.iterrows():
    source_DE_UID = row['source_UID']
    target_DE_UID = row['target_UID']
    COC_filters = row['COC_filter_UIDs'].split(',')

    logger.info("Getting data for DE " + source_DE_UID)

    start_time = time.time()
    sql_view = api.get('sqlViews/' + sql_view_uid, params={'fields':':owner'}).json()
    update_start_year_in_SQL_view(sql_view, year)
    update_DE_UID_in_SQL_view(sql_view, source_DE_UID)
    df = get_csv_from_SQL_view(sql_view)
    export_csv = df.to_csv ('data_' + source_DE_UID + '.csv', index = None, header=True)
    print("--- %s seconds ---" % (time.time() - start_time))

    df = df[df.categoryoptioncombo.isin(COC_filters)]
    f = {'value': 'sum'}
    df = df.groupby(['de', 'pe', 'ou', 'attributeoptioncombo'], as_index=False).agg(f)
    # Get all COCs used

    # des = list(df.de.unique())
    aocs = list(df.attributeoptioncombo.unique())
    # cocs = list(df.categoryoptioncombo.unique())
    ous = list(df.ou.unique())

    # new_unpivoted = df_de.melt(id_vars=['ou'], var_name='pe', value_name='value')
    for ou in ous:
        df_result = pd.DataFrame()
        for aoc in aocs:
            df_aoc = df[df.attributeoptioncombo == aoc]
            #df_de = df_aoc[df_aoc.de == de]
            df_final = df_aoc[df_aoc.ou == ou].sort_values(by=["pe"])
            # Get years available
            df_final['pe'] = df_final['pe'].astype(str)
            periods = list(df_final.pe.unique())
            years = list(set([str(pe)[0:4] for pe in periods]))
            years.sort()

            for year in years:
                df_for_single_year = df_final.copy()
                df_for_single_year = df_for_single_year[df_final['pe'].str.contains(year)]

                df_for_single_year["value"] = df_for_single_year["value"].cumsum()

                if df_result.empty:
                    df_result = df_for_single_year
                else:
                    df_result = pd.concat([df_result, df_for_single_year])

        df_result.loc[df_result["de"] == source_DE_UID, "de"] = target_DE_UID
        df_result = df_result.rename(columns={"de": "dataElement", "ou": "orgUnit", "pe": "period", "attributeoptioncombo": "attributeOptionCombo"})
        result = post_to_server(api, { "dataValues" : df_result.to_dict(orient='records')}, 'dataValueSets')

exit(1)
