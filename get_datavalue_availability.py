from dhis2 import Api, RequestException, load_csv #make sure you have dhis2.py installed, otherwise run "pip3 install dhis2.py"
import json
import logzero
from logzero import logger
from datetime import date, datetime, timedelta
import time
from collections import defaultdict
import pandas as pd
import numpy as np
import statistics
import re
import sys
from dhis2_env import api_from_env

api_source = api_from_env('NIGER_CLEANING')

# setup the logger
log_file="./get_datavalue_stats.log"
logzero.logfile(log_file)


def reindex(json_object, key) :
	new_json = dict()
	for elem in json_object :
		key_value = elem[key]
		#elem.pop(key)
		new_json[key_value] = elem
	return new_json

def analyse_OU(ouLevels, OU, ouUID, ouValues, ouReporting, deUID) :
	for level in sorted(ouLevels) :
		if ouUID in OU[level] :
			ouValues[level][deUID]+=1
			add_reporting_ou_info(level, OU, ouUID, ouReporting, deUID)

def add_reporting_ou_info(level, OU, ouUID, ouReporting, deUID) :
	if level != 1 :
		if ouUID not in ouReporting[level][deUID] :
			ouReporting[level][deUID].append(ouUID)
			add_reporting_ou_info((level-1), OU, OU[level][ouUID]['parent']['id'], ouReporting, deUID)
	else :
		return

def outliers_IQR(value_list):
    number_outliers = 0

    Q1 = np.percentile(value_list, 25)   
    Q3 = np.percentile(value_list, 75)
    IQR = Q3 - Q1

    low_lim = Q1 - 1.5 * IQR 
    up_lim = Q3 + 1.5 * IQR

    for val in value_list:
        if ((val> up_lim) or (val<low_lim)): 
            number_outliers+=1

    return number_outliers

def main():
	#Print DHIS2 Info
	#logger.warning("Server source running DHIS2 version {} revision {}".format(api_source.version, api_source.revision))

	#WHEN
	periodType=sys.argv[1]
	if periodType not in ['Weekly', 'Monthly', 'Quarterly', 'SixMonthly', 'Yearly']:
		print('ERROR: periodType ' + periodType + ' is not correct')
		exit(1)
	startYear=int(sys.argv[2])
	endYear=int(sys.argv[3])
	periods = list()
	only_datasets = True
	logger.info('Using the time period from ' + str(startYear) + ' to ' + str(endYear))

	#WHERE
	OU = dict() # Indexed by level

	#WHAT
	CC = api_source.get('categoryCombos', params={ "paging" : "false", "fields" : "id,name,categoryOptionCombos" }).json()['categoryCombos']
	CC = reindex(CC, 'id')
	defaultCC = ''
	for catcomboUID in CC :
		if CC[catcomboUID]['name'] == 'default' :
			defaultCC = catcomboUID
			break
	if defaultCC == '' :
		logger.warning('Could not find default Category Combo')
	DE = api_source.get('dataElements', params={ "paging" : "false", "fields" : "id,name,categoryCombo,aggregationType,valueType" }).json()['dataElements']
	DE = reindex(DE, 'id')
	de_numeric_types = [ 'INTEGER_POSITIVE', 'INTEGER', 'INTEGER_ZERO_OR_POSITIVE', 'NUMBER', 'PERCENTAGE', 'INTEGER_ZERO_OR_NEGATIVE' ]

	#Get all dataSets in the server - To get just one: "filter":"id:eq:ph2qIseLpDN"
	dataSets = api_source.get('dataSets', params={ "paging" : "false", "fields" : "id,name,dataSetElements,periodType,formType,dataEntryForm,organisationUnits" }).json()['dataSets']

	#Dataframe to store the
	if only_datasets:
		result_df = pd.DataFrame({}, columns=['DataSet', 'ouAssigned', 'numberDataElements'])
	else:
		result_df = pd.DataFrame({}, columns = ['DataSet', 'periodType', 'ouAssigned', 'DataElement', 'Categories'])

	#Get the org unit levels
	ouLevels = api_source.get('organisationUnitLevels', params={ "paging" : "false", "fields" : "id,name,level" }).json()['organisationUnitLevels']
	ouLevels = reindex(ouLevels, 'level')
	#Two variables to store the statistics about total number of values per DE available in a OU level, as well as the total number of distinct OU reporting those values
	ouValues = dict()
	ouReporting = dict()

	rootOU = ''
	for level in sorted(ouLevels) :
		#Initialize the variables. They are first indexed by level, then we will index them by DE UID
		ouValues[level] = dict()
		ouReporting[level] = dict()
		levelName = ouLevels[level]['name']
		OU[level] = api_source.get('organisationUnits', params={ "paging" : "false", "level" : level, "fields" : "id,name,parent" }).json()['organisationUnits']
		OU[level] = reindex(OU[level], 'id')
		if level == 1 :
			#We are going to assume that there is ONLY one OU root at level 1
			for uid in OU[level] :
				if uid == 'yNtPl1eGwar':
					continue
				rootOU = uid
	subperiod = dict()
	subperiod['Weekly'] = list()
	for i in range(1,52):
		subperiod['Weekly'].append('W'+str(i))

	subperiod['Monthly'] = ['01', '02', '03', '04', '05', '06', '07', '08', '09', '10', '11', '12']
	subperiod['Quarterly'] = ['Q1', 'Q2', 'Q3', 'Q4']
	subperiod['SixMonthly'] = ['S1', 'S2']

	total_number_of_periods = 0

	print(result_df.info())

	periods = list()
	for year in range(startYear, (endYear + 1)):
		# TO BE DONE: Manage weekly periods and other frequencies
		if periodType == 'Yearly':
			periods.append(str(year))
			#total_number_of_periods = ((endYear - startYear) + 1)
		else:
			#total_number_of_periods = ((endYear - startYear) + 1) * len(subperiod[periodType])
			for s in subperiod[periodType]:
				periods.append(str(year) + s)
			if periodType == 'Weekly' and date(year, 12, 28).isocalendar()[1] == 53:
				periods.append(str(year) + 'W53')
	result_df = pd.concat([result_df, pd.DataFrame(columns=periods)])

	#start_time = time.time()
	dataset_counter = 0
	for ds in dataSets :
		orgUnitsAssignedToDS = 0
		if 'organisationUnits' in ds and len(ds['organisationUnits']) > 0 :
			orgUnitsAssignedToDS = len(ds['organisationUnits']) 
		if periodType != ds['periodType']:
			continue
		# Build the period variable
		# periods = list()
		# for year in range(startYear, (endYear+1)) :
		# 	#TO BE DONE: Manage weekly periods and other frequencies
		# 	if periodType == 'Yearly':
		# 		periods.append(str(year))
		# 		total_number_of_periods = ((endYear - startYear) + 1)
		# 	else:
		# 		if periodType == 'Quarterly':
		# 			subperiod_list = quarter_list
		# 		elif periodType == 'Monthly':
		# 			subperiod_list = month_list
		# 		total_number_of_periods = ((endYear-startYear) + 1) * len(subperiod_list)
		# 		for subperiod in subperiod_list :
		# 			periods.append(str(year) + subperiod)

		# result_df = pd.concat([result_df, pd.DataFrame(columns=periods)])

		formType = ds['formType']
		dsUID = ds['id']
		dsName = ds['name']
		dataElements = list()
		dataValues = dict()			#Store all values for data element to do min, max, stdev, etc... For ALL org units and not considering periods (NO seasonality)
		dataSetValues = dict()
		categories = dict()			#List of category combos linked to the DE
		numberCOCInCC = dict()		#Number of category options combos which are part of this cat combo linked to the DE
		startPeriod = dict()		#First month/quarter for which we find data
		endPeriod = dict()			#Last month/quarter for which we find data			
		storedBy = dict()			#WHO -> All username of people who entered data for this DE
		lastUpdated = dict()		#Last time a data value for this DE was updated

		if not only_datasets:
			for DSE in ds['dataSetElements'] :
				de = ''
				if 'dataElement' in DSE :
					deUID = DSE['dataElement']['id']
					dataElements.append(deUID)
					de = DE[deUID] #Get all dataElement information
					#Initialize variables
					dataValues[deUID] = dict()
					categories[deUID] = list()
					for level in sorted(ouLevels) :
						if level != 1 :
							ouReporting[level][deUID] = list()
						ouValues[level][deUID] = 0

					startPeriod[deUID] = ''
					endPeriod[deUID] = ''
					lastUpdated[deUID] = ''
					storedBy[deUID] = list()
					#comment[deUID] = list()
					#Check if the Category Combo is specified in the dataElement definition
					numberCOCInCC[deUID] = 1 #Default value, no COCs
					if 'categoryCombo' in de and de['categoryCombo']['id'] != defaultCC :
						categories[deUID].append(de['categoryCombo']['id']) if de['categoryCombo']['id'] not in categories[deUID] else categories[deUID]
						numberCOCInCC[deUID] = len(CC[de['categoryCombo']['id']]['categoryOptionCombos'])

				#Check if Category Combo is specified for the dataElement in the dataSet
				if 'categoryCombo' in DSE and DSE['categoryCombo']['id'] != defaultCC :
					categories[deUID].append(DSE['categoryCombo']['id']) if DSE['categoryCombo']['id'] not in categories[deUID] else categories[deUID]
					numberCOCInCC[deUID] = len(CC[DSE['categoryCombo']['id']]['categoryOptionCombos'])

		else:
			for DSE in ds['dataSetElements'] :
				de = ''
				if 'dataElement' in DSE:
					deUID = DSE['dataElement']['id']
					dataElements.append(deUID)

		for pe in periods:
			logger.info('Processing -- DataSet (' + dsUID + ') = ' + dsName + ' // Period = ' + pe)

			params = {
				'period'  : pe,
				'orgUnit' : rootOU,
				'dataSet' : dsUID,
				'children': 'true'
			}

			#Get data from source server
			retries = 0
			while retries <= 5 :
				try :
					data = api_source.get('dataValueSets', params=params).json()
				except Exception as e:
					logger.error(e)
					time.sleep(1)
					retries+=1
					data = None
				else :
					break

			number_data_values = 0

			if data is not None and 'dataValues' in data :
				number_data_values = len(data['dataValues'])
				if number_data_values > 0 :
					if only_datasets:
						dataSetValues[pe] = str(number_data_values)
						continue
					else:
						for data in data['dataValues']:
							if 'value' in data :
								currentDE = data['dataElement']
								dataValues[currentDE][pe] = '*'


		new_row = dict()
		new_row = {'DataSet':dsName, 'ouAssigned':orgUnitsAssignedToDS, 'numberDataElements':len(dataElements)}
		if only_datasets:
			result_df = result_df.append({**new_row, **dataSetValues}, ignore_index=True)
		else:
			for deUID in dataElements :
				new_row['DataElement'] = DE[deUID]['name']
				# new_row['aggregationType'] = DE[deUID]['aggregationType']
				# new_row['valueType'] = DE[deUID]['valueType']
				# de_is_numeric = False
				# if DE[deUID]['valueType'] in de_numeric_types :
				# 	de_is_numeric = True
				new_row['Categories'] = ''
				if len(categories[deUID]) > 0 :
					for catUID in categories[deUID] :
						if new_row['Categories'] != '' :
							new_row['Categories']+=', '
						new_row['Categories'] =  CC[catUID]['name']

				result_df = result_df.append({**new_row, **dataValues[currentDE]}, ignore_index=True)

		dataset_counter += 1
		if dataset_counter > 1 and dataset_counter % 50 == 0:
			result_df.fillna('')
			export_csv = result_df.to_csv(r'./datavalues_availability-'+str(dataset_counter)+'.csv', index=None, header=True)

	result_df.fillna('')
	export_csv = result_df.to_csv (r'./datavalues_availability-'+periodType+'_'+str(startYear)+'-'+str(endYear)+'.csv', index = None, header=True)

if __name__ == '__main__':
	main()
