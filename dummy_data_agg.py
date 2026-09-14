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
import random
import re
import sys
from calendar import monthrange
from random import randint
from tools.dhis2 import post_to_server
from dhis2_env import api_from_env

api_source = api_from_env('DHIS2_HMIS_STAGING')

# setup the logger
log_file = "./dummy_data_agg.log"
logzero.logfile(log_file)


def reindex(json_object, key):
	new_json = dict()
	for elem in json_object:
		key_value = elem[key]
		# elem.pop(key)
		new_json[key_value] = elem
	return new_json


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

def get_number_days_from_monthly_period(period):
	month = int(period[4:])
	if month == 2:
		return 28
	elif month in [4,6,9,11]:
		return 30
	else:
		return 31



def main():
	# file_name = 'Malaria Stock dummy data'
	# xls = pd.ExcelFile(file_name + '.xlsx')
	#
	# sheet_to_df_map = dict()
	# for sheet_name in xls.sheet_names:
	# 	sheet_to_df_map[sheet_name] = xls.parse(sheet_name)
	# Get all OUs level 2
	ousLevel2 = api_source.get('organisationUnits',
								 params={"paging": "false", "fields": "id",
										 "filter": "level:eq:3"}).json()['organisationUnits']
	facilities_to_use = list()
	facilities_to_use_per_district = dict()
	# 5 facilities per region
	count = 0
	max_number_facilities = 80
	max_factor = 0
	max_variation = 5
	for ou_level_2 in ousLevel2:
		ous = api_source.get('organisationUnits',
								   params={"paging": "false", "fields": "id,level",
										   "filter": "path:like:" + ou_level_2['id']}).json()['organisationUnits']
		if ou_level_2['id'] == 'HZ2oJoxSZGF':
			ou_level_2['id'] = 'HZ2oJoxSZGF'
		facilities_to_use_per_district[ou_level_2['id']] = list()
		for ou in ous:
			if ou['level'] == 4:
				facilities_to_use.append(ou['id'])
				facilities_to_use_per_district[ou_level_2['id']].append(ou['id'])
				count += 1
			if count == max_number_facilities:
				count = 0
				break

		if len(facilities_to_use_per_district[ou_level_2['id']]) == 0:
			facilities_to_use_per_district.pop(ou_level_2['id'], None)

	excel_file = sys.argv[1]
	format = sys.argv[2]
	if '.xlsx' not in excel_file:
		excel_file = excel_file + '.xlsx'
	try:
		xls = pd.ExcelFile(excel_file)
	except FileNotFoundError:
		logger.error('File ' + excel_file + ' does not exist')
		return

	df_result = pd.DataFrame()
	for district in facilities_to_use_per_district:
		logger.info("District " + district + " (num OUs = " + str(len(facilities_to_use_per_district[district])) + ")")
		sheet_counter = 0
		for sheet in xls.sheet_names:
			logger.info("Processing sheet " + sheet)
			if sheet != "Data":
				continue
			#####
			df = pd.read_excel(xls, sheet_name=sheet, na_filter=False, header=None)
			df_result = pd.DataFrame({}, columns=["dataElement", "categoryOptionCombo", "period", "orgUnit", "value"])
			DE_uids = df.loc[1, :].values.tolist()
			COC_uids = df.loc[3, :].values.tolist()
			counter = 1
			for index in range(4,df.shape[0]):
				data_values = df.loc[index, :].values.tolist()
				ou = data_values[0]
				period = data_values[2]
				monthly_date = str(period)
				year = int(monthly_date[:4])
				month = int(monthly_date[4:])
				for i in range(3, len(data_values)):
					value = data_values[i]
					if not isinstance(value, int) or value == "":
						continue
					probability = randint(0, 100)
					if 2 < probability < 5:
						if randint(0,100) < 50:
							value += 1
						else:
							value -= 1
					elif probability <= 2:
						if randint(0,100) < 50:
							value += 2
						else:
							value -= 2
					#if value <= 10:
					#	v = "1"
					days_so_far = list()
					for j in range(0, value):
						day = randint(1, monthrange(year, month)[1])
						while day in days_so_far:
							day = randint(1, monthrange(year, month)[1])
						days_so_far.append(day)
						daily_date = f"{year:04d}{month:02d}{day:02d}"

						df_result.loc[len(df_result)] = [
							DE_uids[i],
							COC_uids[i],
							daily_date,
							ou,
							"1"
						]
					# else:
					# 	number_of_5_steps = int(value / 5)
					# 	list_value_decomposed = [5]*number_of_5_steps
					# 	if value % 5 > 0:
					# 		list_value_decomposed.append(value % 5)
					# 	for value_decomposed in list_value_decomposed:
					# 		df_result.loc[len(df_result)] = [
					# 			DE_uids[i],
					# 			COC_uids[i],
					# 			period,
					# 			random.choice(facilities_to_use),
					# 			value_decomposed
					# 		]
				#result = df_result.to_csv(r'./' + excel_file + '-DATA.csv', index=None, header=True)
				if df_result.shape[0] >= 5000:
					print("Posting batch of 5000")
					result = post_to_server(api_source, {"dataValues": df_result.to_dict(orient='records')}, 'dataValueSets')
					#result = df_result.to_csv(r'./' + excel_file + '-DATA.csv', index=None, header=True)
					df_result = pd.DataFrame({}, columns=["dataElement", "categoryOptionCombo", "period", "orgUnit",
														  "value"])
					counter += 1

			print("Posting last batch")
			result = post_to_server(api_source, {"dataValues": df_result.to_dict(orient='records')}, 'dataValueSets')

			######
			exit(0)
			if sheet_counter >= len(facilities_to_use_per_district[district]):
				logger.warning('Skipping, not enough OUs')
				continue

			de = dict()
			df_columns = list()
			#df = df.rename(columns={"Unnamed: 0": "period"})
			if format == "columns":
				df = pd.read_excel(xls, sheet_name=sheet, na_filter=False, header=None)
				df.at[1, 0] = 'period'
				df = df.drop(labels=0, axis=0)
				df = df.drop(labels=2, axis=0)
				df = df.drop(labels=3, axis=0)
				new_header = df.iloc[0]  # grab the first row for the header
				df = df[1:]  # take the data less the header row
				df.columns = new_header  # set the header row as the df header
				periods = df['period'].tolist()
			elif format == "standard":
				df = pd.read_excel(xls, sheet_name=sheet, na_filter=False)
				#df = df[0:76]
				df.replace('', np.nan, inplace=True)
				df = df.dropna(how='all')
				df = df.dropna(how='all', axis=1)
				df.fillna('', inplace=True)
				df = df.drop(labels='DE name', axis=1)
				df = df.drop(labels='COC name', axis=1)
				df = df.drop(labels='DE + COC', axis=1)
				df = df.rename(columns={"DE UID": "dataelement", "COC UID": "catoptcombo"})

			#for ou in facilities_to_use:
			new_df = df.copy()
			mean = dict()
			std = dict()

			if format == "columns":
				for column in df_columns:
					if column == 'period':
						continue
					mean[column] = int(new_df[column].mean())
					std[column] = int(new_df[column].std())

				new_unpivoted = new_df.melt(id_vars=['period'], var_name='dataelement', value_name='value')
				new_unpivoted = new_unpivoted.replace({"dataelement": de})
			elif format == "standard":
				new_unpivoted = new_df.melt(id_vars=['dataelement', 'catoptcombo'], var_name='period', value_name='value')

			new_unpivoted['orgunit'] = facilities_to_use_per_district[district][sheet_counter]

			# get random number between 0 and 2
			if max_factor != 0:
				factor = random.uniform(0.5, max_factor)
				new_unpivoted['value'] = new_unpivoted['value'] * factor

			if max_variation != 0:
				new_unpivoted['value'] += np.random.randint(0, 10, new_unpivoted.shape[0])

			new_unpivoted['value'] = pd.to_numeric(new_unpivoted['value'])
			new_unpivoted['value'].round(0)
			new_unpivoted['value'] = new_unpivoted.value.astype(int)

			if df_result.empty:
				df_result = new_unpivoted
			else:
				df_result = pd.concat([df_result, new_unpivoted])

			sheet_counter+=1

	max_number_rows = 10000
	df_result = df_result.rename(columns={"dataelement": "dataElement", "orgunit": "orgUnit"})
	if 'catoptcombo' in df_result.columns:
		df_result = df_result.rename(columns={'catoptcombo': 'categoryOptionCombo'})
	#df_result.drop(df_result[df_result.period > 20221113].index, inplace=True)
	if df_result.shape[0] > max_number_rows:
		for i in range(0, df_result.shape[0], max_number_rows):
			if i+max_number_rows > df_result.shape[0]:
				max = df_result.shape[0]
			else:
				max = i+max_number_rows
			df_chunk = df_result.iloc[i:max]
			dataValueSets = {"dataValues": df_chunk.to_dict(orient='records')}
			result = post_to_server(api_source, dataValueSets, 'dataValueSets')


			#result = df_chunk.to_csv(r'./' + excel_file + '-DATA-'+str(i)+'.csv', chunksize=max_number_rows, index=None, header=True)
	else:
		result = df_result.to_csv(r'./' + excel_file + '-DATA.csv', index=None, header=True)

if __name__ == '__main__':
	main()
