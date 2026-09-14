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
from dhis2_env import api_from_env

api_source = api_from_env('SENEGAL_CLEANING')

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
	startYear=2012
	endYear=2022
	periods = list()
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
	dataSets = api_source.get('dataSets', params={ "paging" : "false", "filter": "id:in:[HoP7bWaQEue,MCU9O5hCnax,DHQPxdy0SvP,HBLZ9TSeH7D,XvMz2boLkmZ,HNR2E6e7Z8U,XEbkQ3ca1Bt,eUD1Q2BfaXm,mhCeUKu8TPX,yYNyRqixtPr,HxEz5ka9kGN,mkRvNMKqTDN,sSDHBq99grk,keC54GiDyo5,hQSGTNNt9Hg,cingEsz8xcz,xnaTqpEttWt,IQyIqCHzWrd,BAHCKidaCt9,fyF5tBLuJQd,PZfKrcjffaH,kq1bFbrK1qz,GmwsjztVfT4,c9HBDCaSdfL,Cl8rJWOWPca,P5LapVKM4ZV,V3HLdKoEXJG,K1D87wE51hH,NvmyEb5DFmT,EXvc3TrK1hx,gPwFToCt4JN,VJ3I4wcwzfd,VHQuHNSO4cG,wyJnxahH9iq,oOoEAq9TQyg,bh6RdEYI7ev]", "fields" : "id,name,dataSetElements,periodType,formType,dataEntryForm,organisationUnits" }).json()['dataSets']

	#Dataframe to store the
	result_df = pd.DataFrame({}, columns = ['DataSet', 'periodType', 'formType', 'ouAssigned', 'DataElement', 'valueType', 'aggregationType', 'Categories'])

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
				if uid == 'KEGktGHjhYQ':
					continue
				rootOU = uid
			result_df = pd.concat([result_df,pd.DataFrame(columns=list('T'))])
			result_df.rename(columns={'T' : levelName + '_NumberValues'}, inplace=True)	#Was getting ValueError: Plan shapes are not aligned, not sure why		
		else : 
			result_df = pd.concat([result_df,pd.DataFrame(columns=[levelName + '_NumberValues', levelName + '_NumberReporting'])])
			logger.info('Total number of ' + levelName + ' (level=' + str(level) + '):' + str(len(OU[level])))

	#Data statistic values
	result_df = pd.concat([result_df,pd.DataFrame(columns=['Missing_Values','Null_Values','Min_Value','Max_Value','Mean_Value','Median_Value','Stdev_Value','IQR_Outliers_Number'])])

	# Add extra columns - generic
	result_df = pd.concat([result_df,pd.DataFrame(columns=['StartPeriod', 'EndPeriod'])])


	month_list = ['01', '02', '03', '04', '05', '06', '07', '08', '09', '10', '11', '12']
	quarter_list = ['Q1', 'Q2', 'Q3', 'Q4']
	total_number_of_periods = 0

	print(result_df.info())

	#start_time = time.time()
	for ds in dataSets :
		orgUnitsAssignedToDS = 0
		if 'organisationUnits' in ds and len(ds['organisationUnits']) > 0 :
			orgUnitsAssignedToDS = len(ds['organisationUnits']) 
		periodType = ds['periodType']
		# Build the period variable
		periods = list()
		for year in range(startYear, (endYear+1)) :
			#TO BE DONE: Manage weekly periods and other frequencies
			if periodType == 'Yearly':
				periods.append(str(year))
				total_number_of_periods = ((endYear - startYear) + 1)
			else:
				if periodType == 'Quarterly':
					subperiod_list = quarter_list
				elif periodType == 'Monthly':
					subperiod_list = month_list
				total_number_of_periods = ((endYear-startYear) + 1) * len(subperiod_list)
				for subperiod in subperiod_list :
					periods.append(str(year) + subperiod)

		formType = ds['formType']
		dsUID = ds['id']
		dsName = ds['name']
		dataElements = list()
		dataValues = dict()			#Store all values for data element to do min, max, stdev, etc... For ALL org units and not considering periods (NO seasonality)
		categories = dict()			#List of category combos linked to the DE
		numberCOCInCC = dict()		#Number of category options combos which are part of this cat combo linked to the DE
		startPeriod = dict()		#First month/quarter for which we find data
		endPeriod = dict()			#Last month/quarter for which we find data			
		storedBy = dict()			#WHO -> All username of people who entered data for this DE
		lastUpdated = dict()		#Last time a data value for this DE was updated


		for DSE in ds['dataSetElements'] :
			de = ''
			if 'dataElement' in DSE :
				deUID = DSE['dataElement']['id']
				dataElements.append(deUID)
				de = DE[deUID] #Get all dataElement information
				#Initialize variables
				dataValues[deUID] = list()
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
					for data in data['dataValues']:
						if 'value' in data :
							currentDE = data['dataElement']
							dataValues[currentDE].append(data['value'])

							if startPeriod[currentDE] == '' :
								startPeriod[currentDE] = data['period']
							endPeriod[currentDE] = data['period']

							currentOU = data['orgUnit']
							if currentOU == rootOU :
								ouValues[1][currentDE]+=1
							else :
								analyse_OU(ouLevels, OU, currentOU, ouValues, ouReporting, currentDE)

							#storedBy[currentDE].append(data['storedBy']) if data['storedBy'] not in storedBy[currentDE] else storedBy[currentDE]

							#Customization for Liberia
							# if data['storedBy'] == 'BlueSquare' and 'comment' in data :
							# 	comment_list = data['comment'].split()
							# 	if len(comment_list) > 0 :
							# 		odkForm[currentDE].append(comment_list[0]) if comment_list[0] not in odkForm[currentDE] else odkForm[currentDE]
							# 	if len(comment_list) > 1 :
							# 		sentBy[currentDE].append(comment_list[2]) if comment_list[2] not in sentBy[currentDE] else sentBy[currentDE]
							#Generic
							# if 'comment' in data :
							# 	comment[deUID].append(data['comment'])

							if lastUpdated[currentDE]  == '' :
								lastUpdated[currentDE] = data['lastUpdated'][0:10]
							else :
								previouslastUpdatedDate = datetime.strptime(lastUpdated[currentDE], '%Y-%m-%d')
								lastUpdatedDate = datetime.strptime(data['lastUpdated'][0:10], '%Y-%m-%d')
								if lastUpdatedDate > previouslastUpdatedDate :
									lastUpdated[currentDE] = data['lastUpdated'][0:10]

		new_row = dict()
		new_row = {'DataSet':dsName, 'periodType':periodType, 'formType':formType, 'ouAssigned':orgUnitsAssignedToDS}
		for deUID in dataElements :
			new_row['DataElement'] = DE[deUID]['name']
			new_row['aggregationType'] = DE[deUID]['aggregationType']
			new_row['valueType'] = DE[deUID]['valueType']
			de_is_numeric = False
			if DE[deUID]['valueType'] in de_numeric_types :
				de_is_numeric = True
			new_row['Categories'] = ''
			if len(categories[deUID]) > 0 :
				for catUID in categories[deUID] :
					if new_row['Categories'] != '' :
						new_row['Categories']+=', '
					new_row['Categories'] =  CC[catUID]['name']

			OULevelReportingMoreData = 1
			highestNumberValuesReported = 0
			for level in sorted(ouLevels) :
				levelName = ouLevels[level]['name']
				if level != 1 :
					#Add names for level 2
					# if level == 2:
					# 	listNames = list()
					# 	for ouLevel2UID in ouReporting[level][deUID] :
					# 		listNames.append(OU[level][ouLevel2UID]['name'])
					# 	new_row[levelName + '_NumberReporting'] = ", ".join(sorted(listNames))
					# #Add total for other levels so it does not get messy
					# else :
						new_row[levelName + '_NumberReporting'] = len(ouReporting[level][deUID])
				
				new_row[levelName + '_NumberValues'] = ouValues[level][deUID]
				if ouValues[level][deUID] > highestNumberValuesReported :
					highestNumberValuesReported = ouValues[level][deUID]
					OULevelReportingMoreData = level

			new_row['StartPeriod'] = startPeriod[deUID]
			new_row['EndPeriod'] = endPeriod[deUID]

			#For the missing values we are going to proceed as follows:
			#We focus on the OU level for which there is more data collected. Normally data is only collected at one level (facility, district..)
			#So small number of data in other levels are going to be considered outliers
			#We are also not going to consider the OUs which have never reported any data for this DE
			#So if data was entered at facility level, the formula is as follows: Number of Facilities reporting * Total number of periods * numberCOCInCC[deUID]
			#We multiply for the number of COCs because there will be cases where the DE is disaggregated. So if, for example, we are using <5y and >=5y as COCs
			#rather than expecting 1 value for this DE in X facility for Y period of time, we are expecting 2 values
			#Missing values are Total values expected - the values entered for all facilities
			#Clear values
			new_row['Null_Values'] = new_row['Min_Value'] = new_row['Max_Value'] = new_row['Mean_Value'] = None
			new_row['Median_Value'] = new_row['Stdev_Value'] = new_row['IQR_Outliers_Number'] = None
			if highestNumberValuesReported > 0 and deUID in ouReporting[OULevelReportingMoreData] :
				total_values_expected = len(ouReporting[OULevelReportingMoreData][deUID]) * total_number_of_periods * numberCOCInCC[deUID]
				new_row['Missing_Values'] = total_values_expected - highestNumberValuesReported
				if de_is_numeric == True and len(dataValues[deUID]) > 1 : #Statistics like variance require at least 2 data points
					if DE[deUID]['valueType'] not in ['NUMBER','PERCENTAGE'] :
						numeric_list = [int(float(i)) for i in dataValues[deUID]] #Somehow I got a float value for a DE of not
					else :
						numeric_list = [float(i) for i in dataValues[deUID]]
					new_row['Null_Values'] = len([i for i in numeric_list if int(i) == 0])
					new_row['Min_Value'] = min(numeric_list)
					new_row['Max_Value'] = max(numeric_list)
					new_row['Mean_Value'] = round(statistics.mean(numeric_list),2)
					new_row['Median_Value'] = statistics.median(numeric_list)
					new_row['Stdev_Value'] = round(statistics.stdev(numeric_list),2)
					new_row['IQR_Outliers_Number'] = outliers_IQR(numeric_list)

			# new_row['StoredBy'] = ", ".join(storedBy[deUID])
			# new_row['LastUpdated'] = lastUpdated[deUID]
			#Customization or Liberia
			# new_row['ODK_Form'] = ", ".join(odkForm[deUID])
			# new_row['SentBy'] = ", ".join(sentBy[deUID])
			#new_row['comment'] = ", ".join(comment[deUID])
			result_df = result_df.append(new_row, ignore_index=True)

	export_csv = result_df.to_csv (r'./datavalues_stats.csv', index = None, header=True)

if __name__ == '__main__':
	main()
