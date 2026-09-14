from dhis2 import Api, generate_uid, RequestException, setup_logger, logger
from io import StringIO
import pandas as pd
import numpy as np
import requests as rq
import sys
import time
import glob
import time
import re
import json
from dhis2_env import api_from_env, credentials

mode = 'all' #By default, run all operations
if len(sys.argv) > 1 :
    mode = sys.argv[1]

api_dest = api_from_env('BRUSSELS_BAO')

setup_logger(logfile='opts.log')

def hampel_filter_pandas(input_series, input_periods, window_size, n_sigmas=3):
    '''
    vals: pandas series of values from which to remove outliers
    k: size of window (including the sample; 7 is equal to 3 on either side of value)
    '''

    #Make copy so original not edited
    vals = input_series.copy()  

    #Hampel Filter
    L = 1.4826
    rolling_median = vals.rolling(window=window_size, center=True).median()
    MAD = lambda x: np.median(np.abs(x - np.median(x)))
    rolling_MAD = vals.rolling(window=window_size, center=True).apply(MAD)
    threshold = n_sigmas * L * rolling_MAD
    difference = np.abs(vals - rolling_median)

    '''
    Perhaps a condition should be added here in the case that the threshold value
    is 0.0; maybe do not mark as outlier. MAD may be 0.0 without the original values
    being equal. See differences between MAD vs SDV.
    '''

    outlier_idx = difference > threshold
    # print(outlier_idx.astype(int))
    # print(vals)

    return pd.Series(outlier_idx.astype(int).tolist(), index=[input_periods,input_series])
    # new_series = new_series.astype('int64')
    # new_series[:] = 0
    # new_series[outlier_idx] = 1
    # new_series.index = [input_periods,input_series]
    
    # return new_series 

def hampel_filter_pandas_no_window(input_series, input_periods, n_sigmas=3):
    new_series=input_series.copy()    

    L= 1.4826
    n_sigmas=3
    median=new_series.median()
    difference=np.abs(median-new_series)
    median_abs_deviation=difference.median()
    threshold= n_sigmas *L * median_abs_deviation
    outlier_idx=difference>threshold

    new_series = new_series.astype('int64')
    new_series[:] = 0
    new_series[outlier_idx] = 1
    new_series.index = [input_periods,input_series]
    
    return new_series 

def outliers_modified_z_score(input_series, input_periods):
    threshold = 3.5
    # print(input_series)
    # print(input_series.mean())
    # print(3*input_series.std())

    if (input_series == 0.0).all() :
        outliers = [0] * input_series.shape[0]
    else :
        median_y = np.median(input_series)
        if median_y == 0.0 :
            median_y = 0.00001 #To avoid division by zero
        #print(median_y)
        median_absolute_deviation_y = np.median([np.abs(y - median_y) for y in input_series])
        # print(median_absolute_deviation_y)
        if median_absolute_deviation_y == 0.0 :
            median_absolute_deviation_y = 0.00001        
        modified_z_scores = [0.6745 * (y - median_y) / median_absolute_deviation_y
                             for y in input_series]

        outliers = (np.abs(modified_z_scores) > threshold).astype(int)
        # print(outliers)

    return pd.Series(outliers, index=[input_periods,input_series])

def outliers_IQR(input_series, input_periods):
    input_periods = input_periods.tolist()
    input_series = input_series.tolist()
    n = len(input_series)
    outliers = [0] * n

    Q1 = np.percentile(input_series, 25)   
    Q3 = np.percentile(input_series, 75)
    IQR = Q3 - Q1

    low_lim = Q1 - 1.5 * IQR 
    up_lim = Q3 + 1.5 * IQR

    for idx, val in enumerate(input_series):
        if ((val> up_lim) or (val<low_lim)): 
            outliers[idx] = 1

    return pd.Series(outliers, index=[input_periods,input_series])

def hampel_filter_forloop(input_series, input_periods, window_size, n_sigmas=3, padding = False):
    input_periods = input_periods.tolist()
    input_series = input_series.tolist()
    # print(input_series)

    n = len(input_series)
    k = 1.4826 # scale factor for Gaussian distribution
    
    outliers = [0] * n

    padding=True

    x=input_series.copy()
    if padding == True :
        index_correction=window_size
        x=[0.0]*window_size + x
    else :
        index_correction=0

    #print('Window Size = ' + str(2*window_size+1))
    #print(x)
    # possibly use np.nanmedian
    for i in range(window_size,((n+index_correction) - window_size)):
        #print(' ------ Iteration : ' + str(i))
        #print('Value=' + str(x[i]))
        x0 = np.median(x[(i - window_size):(i + window_size)])
        #print('Median = ' + str(x0))
        S0 = np.median(np.abs(x[(i - window_size):(i + window_size)] - x0))
        #print('stdev: ' + str(S0))
        S0 = k*S0
        if (np.abs(x[i] - x0) > n_sigmas * S0):
            outliers[i-index_correction]=1
            #print('*** Outlier !!!!!')

    # print(outliers)
    
    return pd.Series(outliers, index=[input_periods,input_series])

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

def update_DEG_in_SQL_view(sql_view, DEG_uid) :
    sql_query = sql_view['sqlQuery']
    sql_view_uid = sql_view['id']
    sql_query = re.sub(r"dataelementgroup\s*WHERE\s*uid\s*=\s*'([0-9A-Za-z]{11})", "dataelementgroup WHERE uid = '"+DEG_uid, sql_query)
    sql_view['sqlQuery'] = sql_query
    try:
        print(sql_view)
        response = api.put('sqlViews/' + sql_view_uid, params={'mergeMode': 'REPLACE'}, json=sql_view)
    except RequestException as e:
        logger.error(e)
    else :
        logger.info('SQL View ' + sql_view_uid + ' - DEG succcessfully updated')

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

def get_unpivot_monthly_table_per_DE(df, dataelement) :
    df = df[df.dataelement == dataelement]
    df = df[df['outlier'] == 1]
    df = df[['dps','zs','as','fosa','unit','period','total']]
    #Create multiindex to be able to unpivot with unstack
    df = df.set_index(['dps','zs','as','fosa','unit','period']) # .sort_index()
    df = df.unstack('period')
    #Bring back multiindex to columns
    df.reset_index(inplace=True)
    #Remove unnecessary columns
    del df['dps'], df['as'], df['unit']
    #The following loop has the objective of converting the tuples used now in the columns (2 row header)
    #into one header. The way it is now:
    #               zs     fosa  total                                                                             
    #   period                   201905 201906 201907 201908 201909 201910 201911 201912 202001 202002 202003 202004
    #What we want:
    #               zs     fosa  201905 201906 201907 201908 201909 201910 201911 201912 202001 202002 202003 202004                                                                           
    new_header = list()
    for c in df.columns.tolist() :
        if c[0] == 'total' :
            new_header.append(str(c[1]))
        else :
            new_header.append(str(c[0]))
    df.columns = new_header

    return df

def add_stats(df_orig, df_final, dataelement) :
    df_orig = df_orig[df_orig.dataelement == dataelement] #.dt.date
    df_orig = df_orig[['fosa','period','total']]
    df_group = df.groupby(df_orig.fosa)[['total']].mean().round(2).reset_index()
    df_group.rename(columns={'total': 'mean'}, inplace=True)
    df_final = df_final.merge(df_group, on = 'fosa', how = 'left')
    df_group = df.groupby(df_orig.fosa)[['total']].median().reset_index()
    df_group.rename(columns={'total': 'median'}, inplace=True)
    df_final = df_final.merge(df_group, on = 'fosa', how = 'left')
    df_group = df.groupby(df_orig.fosa)[['total']].min().reset_index()
    df_group.rename(columns={'total': 'min'}, inplace=True)
    df_final = df_final.merge(df_group, on = 'fosa', how = 'left')
    df_group = df.groupby(df_orig.fosa)[['total']].max().reset_index()
    df_group.rename(columns={'total': 'max'}, inplace=True)
    df_final = df_final.merge(df_group, on = 'fosa', how = 'left')

    return df_final

def get_error_rate_quarterly(df, new_header=[]) :
    f = { 'dataelement':'first', 'total':'count', 'outlier':'sum' }
    #Group by data element, adding values for outliwers nd counting occurrences of DE
    df = df.groupby(['dps', 'zs', 'as', 'unit', 'period'], as_index=False).agg(f)
    #Remove data element column, we don't need it anymore
    del df['dataelement']

    #Aggregate quarterly
    df = ( df.assign(period=pd.PeriodIndex(df.period, freq='Q'))
    .groupby(['dps', 'zs', 'as', 'unit','period'])
    .sum()
    .reset_index() )
    #Calculate error rate
    df['error_rate(%)'] = round((df['outlier'] / df['total'])*100,2)
    #r = df.to_csv('test.csv', na_rep='', index = None)
    del df['total'], df['outlier']
    #Set a multiindex to be able to invoke unstack
    df = df.set_index(['dps','zs','as','unit','period'])
    #Unstack -> Place quarters as columns
    df_unstack = df.unstack('period')
    df_unstack.reset_index(inplace=True)
    new_header = list()
    print(df_unstack.columns.tolist())
    for c in df_unstack.columns.tolist() :
        if c[0] == 'error_rate(%)' :
            new_header.append(str(c[1]))
        else :
            new_header.append(str(c[0]))
    df_unstack.columns = new_header

    return df_unstack

def get_table_with_all_metrics(df) :
    x = df[['dps', 'zs', 'as', 'fosa', 'unit', 'dataelement', 'period', 'total']]
    # print(len(x))
    x = x.set_index(['dps','zs', 'as','fosa', 'unit', 'dataelement', 'period']).unstack('period').stack(dropna=False).sort_index(level=1).reset_index()
    export_csv = x.to_csv ('./test_data-pivot.csv', index = None, header=True)

    x['empty'] = x.total.isnull().astype(int)  
    del x['total']
    f = { 'dataelement':'first', 'empty':'sum' }
    x = x.groupby(['dps', 'zs', 'as', 'fosa','unit', 'period'], as_index=False).agg(f)
    #x['empty'] = x.groupby(['dps','zs', 'as','unit', 'period'])['empty'].transform('sum').astype(int)    
    del x['dataelement']

    df['zero'] = (df['total'] == 0.0).astype(int)
    f = { 'dataelement':'first', 'total':'count', 'outlier':'sum', 'zero':'sum' }
    df_all = df.groupby(['dps', 'zs', 'as', 'fosa', 'unit', 'period'], as_index=False).agg(f)
    df_all.rename(columns={'total':'entered'}, inplace=True)
    del df_all['dataelement']

    df_final = x.merge(df_all, on = ['dps','zs', 'as','fosa','unit', 'period'], how = 'left')

    return df_final

def post_to_server(jsonObject, apiObject = 'metadata', strategy = 'CREATE_AND_UPDATE') :
    try:
        response = api_dest.post(apiObject, params={'mergeMode': 'REPLACE', 'importStrategy': strategy}, json=jsonObject)
    except Exception as err:
        # Print errors returned from DHIS2
        logger.error("POST failed with error " + str(err) + "(server: " + server + ")")
        return False
    else:
        text = json.loads(response.text)
        if(text['status'] == 'ERROR') :
            logger.error(text)
            return False
        else :
            if apiObject == 'metadata' :
                logger.info("metadata imported in " + server + " " + text['status'] + " " + json.dumps(text['stats']))
            else :
                logger.info("data imported in " + server + " " + text['status'] + " " + json.dumps(text['importCount']))
                print(text)
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

data_csv_file = ''
data_csv_file_outliers = ''
report_csv_file = ''
df_ = pd.DataFrame({})
DEG_uid = '' #sys.argv[4]
VRG_uid = 'h7XsqDiP48b' #'Z3KBWHAxg6o' #'haylJTO85G2' 
server='snis-rdc' #'ima-assp'
dhis_url, dhis_uname, dhis_pwd = credentials('SNIS_RDC')
api = Api(dhis_url, dhis_uname, dhis_pwd)
list_DE = ['Données saisies', 'Données vides', 'Valeurs nulles', 'Valeurs aberrantes', 'Règles de validation cassées']
start_date ='2019-11-01'
start_date_for_analysis ='2019-04-01'
end_date = '2020-06-30'

if mode == 'all' or mode == 'sql_view' :
    print('Getting data from SQLView...')
    start_time = time.time()
    sql_view_uid='sWcwiPwApTX' #With orgUnit
    #sql_view_uid='Xf25BFrgMW3'
    sql_view = api.get('sqlViews/' + sql_view_uid, params={'fields':':owner'}).json()
    data_csv_file='data.csv'
    update_period_in_SQL_view(sql_view, start_date, end_date)
    update_DEG_in_SQL_view(sql_view, DEG_uid)
    df = get_csv_from_SQL_view(sql_view)
    export_csv = df.to_csv (data_csv_file, index = None, header=True)
    print("--- %s seconds ---" % (time.time() - start_time))

if mode == 'all' or mode == 'calcul_outliers' :
    if data_csv_file == '' :
        if len(sys.argv) > 2 :
            data_csv_file = sys.argv[2]
            df = pd.read_csv(data_csv_file, low_memory=False)
        else :
            logger.error('Please provide file name for data csv')
            exit(0)

    #print(df.info())
    df = df.sort_values(by=['period', 'dataelement', 'unit'])

    #df["Total"] = pd.to_numeric(df.Total)

    periods = df.period.unique()
    units = df.unit.unique()
    des = df.dataelement.unique()
    # print('Periods available :')
    # print(periods)
    print('Total number of DEs : ' + str(len(des)))
    print('Total number of FOSAs : ' + str(len(units)))

    # des = ['nx Préservatif masculin - quantité administrée CS (A)', 'nx Nouvelle acceptante PF (total)']
    # units = ['ks Nsumbula Centre de Sante de Reference', 'ks Nsumbula Centre de Sante']

    # df = df[(df.unit.isin(units)) & (df.dataelement.isin(des))]
    # del df['outlier']
    # df = df.sort_values(by=['unit', 'dataelement', 'period'])
    # export_csv = df.to_csv ('./test_data.csv', index = None, header=True)
    # exit(0)

    #Create new column for Outlier result, default value to 0 (False, not outlier)
    #df = df.set_index('period')
    start_time = time.time()
    columns = df.columns.tolist()
    columns.append('outlier')
    print('Calculating outliers... ')

    # windows = [3] #[ 1, 2, 3, 4, 5, 6]

    # df_result = pd.DataFrame()

    # for window_size in windows :
    #     print('Window size = ' + str(window_size))
    #     df_out = df.groupby(['dps','zs','as','fosa','unit','dataelement']).apply(lambda x: hampel_filter_forloop(x['total'], x['period'], window_size)).to_frame('outlier').reset_index()
    #     df_out.columns = columns
    #     df_out['period'] = pd.to_datetime(df['period'], format="%Y%m")
    #     df_out = df_out[df_out.period >= start_date_for_analysis]
    #     df_out['period']= df_out['period'].dt.strftime('%Y%m')
    #     df_out = df_out[['period','outlier']]
    #     df_out = df_out.groupby(df_out.period)[['outlier']].sum().reset_index()
    #     df_out = df_out.rename(columns={'outlier': 'Hampel_k_'+str(window_size)})

        # if window_size == 1 :
        #     df_result = df_out
        # else :
        #     df_result = pd.merge(df_result, df_out, on='period')

    df_out = df.groupby(['dps','zs','as','fosa','unit','dataelement']).apply(lambda x: outliers_IQR(x['total'], x['period'])).to_frame('outlier').reset_index()
    df_out.columns = columns
    df_out['period'] = pd.to_datetime(df_out['period'], format="%Y%m")
    df_out = df_out[df_out.period >= start_date_for_analysis]
    df_out['period']= df_out['period'].dt.strftime('%Y%m')
    #df = df.sort_values(by=['unit','dataelement','period'])
    # df_out = df_out[['period','outlier']]
    # df_out = df_out.groupby(df_out.period)[['outlier']].sum().reset_index()
    # df_out = df_out.rename(columns={'outlier': 'Modified_Z_Score'})
    # df_result = df_out

    # export_csv = df_result.to_csv ('./mod_z_analysis.csv', index = None, header=True)




    data_csv_file_outliers = data_csv_file.replace('.csv', '-outliers.csv')

    export_csv = df_out.to_csv (data_csv_file_outliers, index = None, header=True)

    print("--- %s seconds ---" % (time.time() - start_time))
    

    #   columns = df.columns
    # df_final = df.groupby(['dps','zs','as','unit','dataelement'])
    # .apply(lambda x: hampel_filter_forloop_no_window_v4(x['total'], x['period']))
    # .to_frame('outlier')
    # .reset_index()

    # df_final = columns.append('outlier')
    # print(df_final)

    # df2 = df.set_index(['dps','zs','as','unit','dataelement','period'])
    # print(df2.groupby(level=df2.index.names.difference(['period']))['total']
    #     .apply(func=hampel_filter_forloop_no_window_v3)
    #     .to_frame('outlier')
    #     .reset_index())


if mode == 'all' or mode == 'report' :
    if data_csv_file_outliers == '' :
        if len(sys.argv) > 2 :
            data_csv_file_outliers = sys.argv[2]
            df_out = pd.read_csv(data_csv_file_outliers, low_memory=False)
        else :
            logger.error('Please provide file name for outliers csv')
            exit(0)

    #df_out['period'] = pd.to_datetime(df['period'], format="%Y%m")
    #all_dates = pd.date_range(start=df.period.min(), end=df.period.max(), freq='M').to_period('m')

    # cs = 'ks Nsumbula Centre de Sante de Reference'
    # de = 'nx Préservatif masculin - quantité administrée CS (A)'
    start_time = time.time()
    print('Calculating metrics... ')
    df_final = get_table_with_all_metrics(df_out)

    data_csv_file_report = data_csv_file_outliers.replace('-outliers.csv', '-report.csv')

    export_csv = df_final.to_csv (data_csv_file_report, index = None, header=True)
    print("--- %s seconds ---" % (time.time() - start_time))



#if mode == 'all' or mode == 'val_rules' :
# if mode == 'val_rules' :
#     if mode == 'val_rules' : #Try to get metadata definitions and report
#         if len(sys.argv) > 2 :
#             report_csv_file = sys.argv[2]
#             df_final = pd.read_csv(report_csv_file, low_memory=False)
#         else :
#             logger.error('Please provide file name for the csv report')
#             exit(0)
#     ouRoot = 'ymGeqzoPhN3' #Normally the country

#     val_rules_payload = {
#         "startDate":start_date,
#         "endDate":end_date,
#         "ou":ouRoot,
#         "notification":"false",
#         "persist":"false",
#         "vrg":VRG_uid
#     }

#     try:
#         response = api.post('dataAnalysis/validationRules', params={}, json=val_rules_payload).json()
#     except Exception as err:
#         logger.error("POST failed with error " + str(err) + "(server: " + server + ")")
#         pass
#     else:
#         if len(response) > 0 : #Some validation rules were broken
#             df = pd.DataFrame(response)
#             #print(df)
#             df = df.groupby(['organisationUnitId','periodId']).size().to_frame(name = 'vl_broken').reset_index()
#             df.rename(columns={'organisationUnitId':'unit', 'periodId':'period'}, inplace=True)
#             df['period'] = df['period'].astype(int)
#             #print(df)
#             df_final = df_final.merge(df, on = ['unit', 'period'], how = 'left')
#             print('Validation Rules = ' + str(df['vl_broken'].count()) + ' Merged = ' + str(df_final['vl_broken'].count()))
#             #print(df_final)
#             export_csv = df_final.to_csv (report_csv_file, index = None, header=True)
#         else :
#             print('No validation rules broken')

if mode == 'completeness' :

    dsUID = 'pMbC0FJPkcm'
    deUID = 'RmY0eloFPTG'
    ouZS = api.get('organisationUnits', params={ "paging" : "false", "level" : "3", "fields" : "id,name" }).json()['organisationUnits']
    year = '2020'
    months = ['01', '02', '03', '04', '05', '06']

    result_df = pd.DataFrame({}, columns = ['period','orgUnit','value'])

    for zs in ouZS :
        zsUID = zs['id']
        for m in months :
            period = year+m
            params = {
                'period'  : period,
                'orgUnit' : zsUID,
                'dataSet' : dsUID,
                'children': 'true'
            }
            logger.info('Getting completeDataSetRegistrations for DS ' + dsUID + ' in OU ' + zs['name'] + ' (' + zsUID + ')' + ' for the month ' + period)
            completeness = api.get('completeDataSetRegistrations', params=params).json()

            if 'completeDataSetRegistrations' not in completeness :
                logger.warning('No completeDataSetRegistrations for ' + zs['name'] + ' in ' + period)
                continue
            else :
                completeness = completeness['completeDataSetRegistrations']

            result_df = result_df.append({'period':period, 'orgUnit':zsUID, 'value':len(completeness)}, ignore_index=True)

    result_df['dataElement'] = deUID
    print(result_df.head(20))
    logger.info('Storing result in DE ' + deUID)
    result = post_to_server({ "dataValues" : result_df.to_dict(orient='records')}, 'dataValueSets')

if mode == 'timeliness' :

    #Get table from sql view
    print('Getting data from SQLView...')
    start_time = time.time()
    sql_view_uid='PvyMiYbTsRr' #With orgUnit
    sql_view = api.get('sqlViews/' + sql_view_uid, params={'fields':':owner'}).json()
    data_csv_file='data-timeliness.csv'
    update_period_in_SQL_view(sql_view, start_date, end_date)
    df = get_csv_from_SQL_view(sql_view)
    export_csv = df.to_csv (data_csv_file, index = None, header=True)
    print("--- %s seconds ---" % (time.time() - start_time))

    #df = pd.read_csv('./data-timeliness.csv')
    dataSet_csv="datavalues_stats-A-_Services_de_Base.csv"
    df_ds = pd.read_csv(dataSet_csv)
    df_ds.dropna(inplace=True)
    print(df_ds.info())
    del df_ds['DataSet'], df_ds['Entered'], df_ds['Empty'], df_ds['Empty(% Total)'], df_ds['Zeros'], df_ds['Zeros(% Total)']

    #Get ZS UIDs
    ouZS = api.get('organisationUnits', params={ "paging" : "false", "level" : "3", "fields" : "id,name" }).json()['organisationUnits']
    name_to_uid = dict()
    for ou in ouZS :
        name_to_uid[ou['name']] = ou['id']


    print(df)
    print(df_ds)

    df_final = df_ds.merge(df, on = ['dps', 'zs', 'as', 'fosa', 'unit', 'period'], how = 'left')
    df_final = df_final.fillna(0)
    df_final['unit'] = df_final['zs']
    df_final = df_final.replace({"unit": name_to_uid})
    df_final.rename(columns={'unit' : 'orgUnit'}, inplace=True)
    df_in_time = df_final[df_final.dayslate <= 7.0]
    f = {  'fosa':'first', 'orgUnit':'first', 'dayslate':'count' }
    df_group = df_in_time.groupby(['dps', 'zs', 'period'], as_index=False).agg(f)
    DVS = get_datavaluesets_from_column(df_group, 'dayslate', 'Hv0xLw8BvNk')
    result = post_to_server(DVS, 'dataValueSets')

    df_late = df_final[df_final.dayslate == 8.0]
    df_group = df_late.groupby(['dps', 'zs', 'period'], as_index=False).agg(f)
    DVS = get_datavaluesets_from_column(df_group, 'dayslate', 'a9KivWCSVHW')
    result = post_to_server(DVS, 'dataValueSets')




# Validation rules calculation and pushing to server, all in one - metadata needs to be already there
if mode == 'val_rules' :

    ouRoot = 'ymGeqzoPhN3' #Normally the country
  
    #rules = ['haylJTO85G2', 'XTdEc6JfZyi', 'h7XsqDiP48b', 'e25XIuBERXF']
    #des = ['WN7ElrqA4n1', 'RzlIFrlys9L', 'x2XApLMkfsL', 'rHM4DdG9qpg']

    rules = ['e25XIuBERXF']
    des = ['rHM4DdG9qpg']

    for i in range(0,3) :
        print(i)
        val_rules_payload = {
            "startDate":start_date,
            "endDate":end_date,
            "ou":ouRoot,
            "notification":"false",
            "persist":"false",
            "vrg":rules[i]
        }
        print(val_rules_payload)

        try:
            response = api.post('dataAnalysis/validationRules', params={}, json=val_rules_payload).json()
        except Exception as err:
            logger.error("POST failed with error " + str(err) + "(server: " + server + ")")
            pass
        else:         
            if len(response) > 0 : #Some validation rules were broken
                print(len(response))
                df = pd.DataFrame(response)
                print(df.head(10))
                df = df.groupby(['organisationUnitId','periodId']).size().to_frame(name = 'vl_broken').reset_index()
                df.rename(columns={'organisationUnitId':'unit', 'periodId':'period'}, inplace=True)
                df['period'] = df['period'].astype(int)
                #print(df)
                df_final = df[['unit','period','vl_broken']]
                #df_final = df_final.merge(df, on = ['unit', 'period'], how = 'left')
                print('Validation Rules = ' + str(df['vl_broken'].count()) + ' Merged = ' + str(df_final['vl_broken'].count()))
                #print(df_final)
                export_csv = df_final.to_csv ('./val_rules_broken_' + rules[i] + '.csv', index = None, header=True)
                #export_csv = df_final.to_csv (report_csv_file, index = None, header=True)

                df_final.rename(columns={'unit' : 'orgUnit'}, inplace=True)
                #df_final['period']= pd.to_datetime(df_final['period']).dt.strftime('%Y%m')\q


                DVS = get_datavaluesets_from_column(df_final, 'vl_broken', des[i])
                result = post_to_server(DVS, 'dataValueSets')
            else :
                print('No validation rules broken')

if mode == 'all' or mode == 'create_metadata' :

    DEG = api.get('dataElementGroups/' + DEG_uid, params={'fields':'id,shortName,name'}).json()
    new_DEG_name = DEG['name'] + ' - Qualité des Données'
    if len(DEG['shortName']) <= 29 :
        new_DEG_shortName = DEG['shortName'] + ' - Qualité de Données'
    else :
        new_DEG_shortName = DEG['shortName'] + ' - QD'

    acronym = "".join(e[0] for e in DEG['shortName'].split()) + ' - QD'

    degJson = {
        'id'                : generate_uid(),
        'name'              : new_DEG_name,
        'shortName'         : new_DEG_shortName,
        'aggregationType'   : 'SUM',
        'dataElements'      : []
    }
    deJsonTemplate = { 
        'id'                : '',
        'name'              : '',
        'shortName'         : '',
        'aggregationType'   : 'SUM',
        'domainType'        : 'AGGREGATE',
        'dataElementCategoryCombo' : '',
        'valueType'         : 'INTEGER_ZERO_OR_POSITIVE',
        'zeroIsSignificant' : 'true'
    }
    metadata = {}
    metadata['dataElements'] = list()
    metadata['dataElementGroups'] = list()
    metadata['dataElementGroups'].append(degJson)
    for de in list_DE :
        deJson = deJsonTemplate.copy()
        uid = generate_uid()
        deJson['id'] = generate_uid()
        deJson['name'] = DEG['shortName'] + ' - ' + de
        deJson['shortName'] = acronym + ' - ' + de
        metadata['dataElements'].append(deJson)

        metadata['dataElementGroups'][0]['dataElements'].append({'id':deJson['id']})

    #print(metadata)

    result = post_to_server(metadata)

    if result == True :
        with open('metadata.json', 'w') as fp:
            json.dump(metadata, fp, sort_keys=True, indent=4, separators=(',', ': '))

if mode == 'all' or mode == 'push_data' :

    columns_DE = ['entered', 'empty', 'zero', 'outlier', 'vl_broken']

    if mode == 'push_data' : #Try to get metadata definitions and report
        if len(sys.argv) > 3 :
            report_csv_file = sys.argv[2]
            df_final = pd.read_csv(report_csv_file, low_memory=False)
            metadata_json_file = sys.argv[3]
            with open(metadata_json_file) as json_file:
                metadata = json.load(json_file)
        else :
            logger.error('Please provide file name for the csv report and metadata definitions')
            exit(0)

        #Change column unit to orgUnit   
        df_final.rename(columns={'unit' : 'orgUnit'}, inplace=True)
        #df_final['period']= pd.to_datetime(df_final['period']).dt.strftime('%Y%m')

        dict_DE = {}
        for i in range(0,len(columns_DE)) :
            for de in metadata['dataElements'] :
                if list_DE[i] in de['name'] :
                    dict_DE[columns_DE[i]] = de['id']

        for col_name in dict_DE :
            DVS = get_datavaluesets_from_column(df_final, col_name, dict_DE[col_name])
            result = post_to_server(DVS, 'dataValueSets') 

# if mode == 'all' or mode == 'push_data' :

#     if mode == 'push_data' : #Try to get metadata definitions and report
#         if len(sys.argv) > 2 :
#             report_csv_file = sys.argv[2]
#             df_final = pd.read_csv(report_csv_file, low_memory=False)
#         else :
#             logger.error('Please provide file name for the csv report and metadata definitions')
#             exit(0)

#         #Change column unit to orgUnit   
#         df_final.rename(columns={'unit' : 'orgUnit'}, inplace=True)
#         #df_final['period']= pd.to_datetime(df_final['period']).dt.strftime('%Y%m')

#         DVS = get_datavaluesets_from_column(df_final, 'vl_broken', 'h7XsqDiP48b')
#         result = post_to_server(DVS, 'dataValueSets') 


if mode == 'get_excel_de' :
    if len(sys.argv) > 2 :
        data_csv_file = sys.argv[2]
        df = pd.read_csv(data_csv_file, low_memory=False)
    else :
        logger.error('Please provide file name with the outlier data in csv format')
        exit(0)

    excel_file ='./outliers_DEs.xlsx'
    writer = pd.ExcelWriter("./outliers_DEs.xlsx", engine='openpyxl')
    logger.info("Generating file " + excel_file + " with results per each DE...")
    for de in df.dataelement.unique() :
        df_de = get_unpivot_monthly_table_per_DE(df, de)
        if df_de.empty != True :
            df_final = add_stats(df, df_de, de)
            df_final.to_excel(writer, sheet_name=de, index=False)
        else :
            logger.warning('No outliers for DE: ' + de)

    writer.save()

if mode == 'open_filter_by_de_ou' :
    des = ['nx Préservatif masculin - quantité administrée CS (A)', 'nx Nouvelle acceptante PF (total)','nx Collier du cycle - quantité administrée ADBC (B)']
    units = ['ks Nsumbula Centre de Sante de Reference', 'ks Nsumbula Centre de Sante','kr Bakwa Muanza Centre de Sante']
    #des = ['nx Collier du cycle - quantité administrée ADBC (B)']
    #units = ['kr Bakwa Muanza Centre de Sante']
    #des = ['nx Préservatif masculin - quantité administrée CS (A)']
    #units = ['ks Nsumbula Centre de Sante de Reference', 'ks Nsumbula Centre de Sante']

    csv_file = sys.argv[2]
    df = pd.read_csv(csv_file, low_memory=False)
    df = df[(df.fosa.isin(units)) & (df.dataelement.isin(des))]
    df['period'] = pd.to_datetime(df['period'], format="%Y%m")
    df = df[df.period >= '201901']
    df['period']= df['period'].dt.strftime('%Y%m')
    df = df.sort_values(by=['unit','dataelement','period'])

    #df['entered'] = df['entered'].astype('Int64')
    #df['zero'] = df['zero'].astype('Int64')
    #df['outlier'] = df['outlier'].astype('Int64')
    print(df)
    data_csv_out = csv_file.replace('.csv', '-filtered.csv')
    export_csv = df.to_csv (data_csv_out, index = None, header=True)  

