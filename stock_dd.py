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
from dhis2_env import api_from_env

api_source = api_from_env('DHIS2_HMIS_DEV')

# setup the logger
log_file = "./stock_dd.log"
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
										 "filter": "level:eq:2"}).json()['organisationUnits']
	facilities_to_use = list()
	# 5 facilities per region
	count = 0
	max_number_facilities = 5
	for ou_level_2 in ousLevel2:
		ous = api_source.get('organisationUnits',
								   params={"paging": "false", "fields": "id,level",
										   "filter": "path:like:" + ou_level_2['id']}).json()['organisationUnits']
		for ou in ous:
			if ou['level'] == 4:
				facilities_to_use.append(ou['id'])
				count += 1
			if count == max_number_facilities:
				count = 0
				break

	stock_prefixes = ['MAL - ACT', 'MAL - Artesunate', 'MAL - LLIN', 'MAL - Primaquine',
					  'MAL - RDT', 'MAL - Sulfadoxine-pyrimethamine', 'TB - DR-TB drugs', 'TB - DS-TB drugs',
					  'TB - GeneXpert Cartridges', 'HIV - HIV test kits',  'HIV - HIV treatment courses']

	df_generic_stock = pd.read_csv('generic_stock.csv')

	for stock_prefix in stock_prefixes:
		logger.info("Processing " + stock_prefix)
		df = df_generic_stock.copy()
		de = dict()
		df_columns = list()
		df_result = pd.DataFrame()
		df = df.rename(columns={"Unnamed: 0": "period"})
		periods = df['period'].tolist()
		number_facilities_stock_days = dict()
		for period in periods:
			number_facilities_stock_days[period] = 0
		for type_name in df.columns:
			if type_name == 'period':
				continue
			de_name = stock_prefix + ' ' + type_name
			df_columns.append(type_name)
			try:
				dataElement = api_source.get('dataElements',
											 params={"paging": "false", "fields": "id,name",
													 "filter": "name:like:" + de_name}).json()['dataElements']
			except RequestException as e:
				logger.error('Server return ' + str(e.code))
				return False
			else:
				if len(dataElement) == 0:
					de_name = stock_prefix + 's ' + type_name
					try:
						dataElement = api_source.get('dataElements',
													 params={"paging": "false", "fields": "id,name",
															 "filter": "name:like:" + de_name}).json()['dataElements']
					except RequestException as e:
						logger.error('Server return ' + str(e.code))
						return False
					if len(dataElement) == 0:
						logger.error('Not found!! ' + de_name)
					else:
						de[type_name] = dataElement[0]['id']
				else:
					de[type_name] = dataElement[0]['id']

		for ou in facilities_to_use:
			new_df = df.copy()
			mean = dict()
			std = dict()

			for column in df_columns:
				mean[column] = int(new_df[column].mean())
				std[column] = int(new_df[column].std())

			# stock on hand = opening + received - issued - redistributed - discarded
			previous_stock_on_hand = 0
			stock_on_hand = 0
			is_stock_out = False
			stock_out_periods = 0
			max_stock_out_periods = 3
			received_column_name = ""
			for index in range(0, df.shape[0]):
				for column in df_columns:
					new_df.at[index, column] = new_df.iloc[index][column] + random.randint(-1*std[column], 1*std[column])
					if 'opening' in column:
						if index == 0:
							stock_on_hand = new_df.iloc[index][column]
						else:
							new_df.at[index, column] = stock_on_hand
					elif 'received' in column:
						stock_on_hand = stock_on_hand + new_df.iloc[index][column]
						if received_column_name == "":
							received_column_name = column
					elif 'issued' in column or 'redistributed' in column or 'discarded' in column:
						if 'issued' in column:
							column_issued = column
						stock_on_hand = stock_on_hand - new_df.iloc[index][column]
					elif 'on hand' in column:
						if stock_on_hand < 0:
							print('stock on hand negative')
							while stock_on_hand < 0:
								stock_on_hand += 5
								new_df.at[index, received_column_name] = new_df.iloc[index][received_column_name] + 5
							print('new stock at hand : ' + str(stock_on_hand))
						if not is_stock_out and random.choice([1,2,3,4,5,6,7,8,9,10]) == 5:
							is_stock_out = True
							# Determine how many periods of stock out
							stock_out_periods = random.randint(1, max_stock_out_periods)
							print('stock out periods = ' + str(stock_out_periods))
						if stock_out_periods > 0:
							print('period ' + str(stock_out_periods))
							new_df.at[index, column_issued] += stock_on_hand
							stock_on_hand = 0
							stock_out_periods -= 1
						new_df.at[index, column] = stock_on_hand
					elif 'out day' in column:
						new_df[new_df < 0] = 0
						if stock_on_hand == 0:
							print('stock out at ' + str(new_df.iloc[index]['period']))
							new_df.at[index, column] = random.randint(1, get_number_days_from_monthly_period(str(new_df.iloc[index]['period'])))
							number_facilities_stock_days[new_df.at[index, 'period']] += 1
						else:
							new_df.at[index, column] = 0
			result = new_df.to_csv(r'./' + stock_prefix + '.csv', index=None, header=True)
			exit(1)
			new_unpivoted = new_df.melt(id_vars=['period'], var_name='dataelement', value_name='value')
			new_unpivoted = new_unpivoted.replace({"dataelement": de})
			new_unpivoted['orgunit'] = ou

			if df_result.empty:
				df_result = new_unpivoted
			else:
				df_result = pd.concat([df_result, new_unpivoted])

		result = df_result.to_csv(r'./' + stock_prefix + '.csv', index=None, header=True)


if __name__ == '__main__':
	main()
