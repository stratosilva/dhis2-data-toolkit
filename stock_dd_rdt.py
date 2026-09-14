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
	# ousLevel2 = api_source.get('organisationUnits',
	# 							 params={"paging": "false", "fields": "id",
	# 									 "filter": "level:eq:2"}).json()['organisationUnits']
	# facilities_to_use = list()
	# # 5 facilities per region
	# count = 0
	# max_number_facilities = 5
	# for ou_level_2 in ousLevel2:
	# 	ous = api_source.get('organisationUnits',
	# 							   params={"paging": "false", "fields": "id,level",
	# 									   "filter": "path:like:" + ou_level_2['id']}).json()['organisationUnits']
	# 	for ou in ous:
	# 		if ou['level'] == 4:
	# 			facilities_to_use.append(ou['id'])
	# 			count += 1
	# 		if count == max_number_facilities:
	# 			count = 0
	# 			break

	# RDT_tests_uid = 'pmRn0GsUfr6'
	# periods = list()
	# for year in ['2020', '2021', '2022']:
	# 	for i in range(1,13):
	# 		periods.append(year + str(i).zfill(2))
	# 		if year == '2022' and i == 2:
	# 			break
	# ousLevel4 = api_source.get('organisationUnits',
	# 						   params={"paging": "false", "fields": "id",
	# 						   "filter": "level:eq:4"}).json()['organisationUnits']
	# ousLevel4 = reindex(ousLevel4, 'id')
	# for ou in ousLevel4:
	# 	for pe in periods:
	# 		try:
	# 			print("de="+RDT_tests_uid+"?pe="+pe+"?ou="+ou)
	# 			dataValues = api_source.get('dataValues',
	# 										params={"de": RDT_tests_uid, "pe": pe, "ou": ou}).json()
	# 		except RequestException as e:
	# 			pass
	# 		else:
	# 			print(dataValues)
	# exit(0)

	df_rdt = pd.read_csv('RDT_tests.csv')
	periods_tmp = list(df_rdt.pe.unique())
	periods = list()
	for pe in periods_tmp:
		if str(pe)[0:4] not in ['2017', '2018']:
			periods.append(str(pe))
	periods.sort()
	facilities_to_use = list(df_rdt.ou.unique())

	stock_prefixes = ['MAL - RDT']

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
		column_issued_name = ""
		received_column_name = ""
		for period in periods:
			number_facilities_stock_days[period] = 0
		for type_name in df.columns:
			if type_name == 'period':
				continue
			if 'issued' in type_name:
				column_issued_name = type_name
			if 'received' in type_name:
				received_column_name = type_name
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
		print(de)

		for ou in facilities_to_use:
			new_df = df.copy()
			mean = dict()
			std = dict()

			# Apply a correction factor
			# Calculate the mean for the facility for RDT_tests
			df_rdt_facility = df_rdt[df_rdt.ou == ou]
			mean_rdt_for_facility = df_rdt_facility['value'].mean()
			for column in df_columns:
				if 'issue' in column:
					# Let's see how far we are from the current amount of RDT tests
					issued_value_mean = int(new_df[column].mean())
					correction_factor = mean_rdt_for_facility / issued_value_mean
					break

			for column in df_columns:
				new_df[column] = (new_df[column]*correction_factor).astype(int)
				mean[column] = int(new_df[column].mean())
				std[column] = int(new_df[column].std())

			# stock on hand = opening + received - issued - redistributed - discarded
			previous_stock_on_hand = 0
			stock_on_hand = 0
			is_stock_out = False
			stock_out_periods = 0
			max_stock_out_periods = 3
			start_opening_from_scratch = True
			for index in range(0, df.shape[0]):
				# Check if there is data for the period and the OU
				period = new_df.iloc[index]['period']
				if str(int(period)) == '202105':
					a = 2
				filtered_df_rdt = df_rdt[(df_rdt.pe == period) & (df_rdt.ou == ou)]
				if filtered_df_rdt.shape[0] != 1:
					new_df.iloc[index] = np.nan
					new_df.at[index, 'period'] = period
					start_opening_from_scratch = True
					continue
				else:
					RDT_test = int(filtered_df_rdt.value.tolist()[0])
					print('Found value ' + str(RDT_test))

				for column in df_columns:
					new_value = new_df.iloc[index][column] + random.randint(-1*std[column], 1*std[column])
					new_df.at[index, column] = new_value
					if 'opening' in column:
						if index == 0 or start_opening_from_scratch:
							stock_on_hand = new_df.iloc[index][column]
							start_opening_from_scratch = False
						else:
							new_df.at[index, column] = stock_on_hand
					elif 'received' in column:
						stock_on_hand = stock_on_hand + new_df.iloc[index][column]
					elif 'issued' in column or 'redistributed' in column or 'discarded' in column:
						if 'issued' in column:
							column_issued_name = column
							if RDT_test > new_value:
								# Adjust issued
								print('Adjust issued ' + str(RDT_test) + ' > ' + str(new_value))
								# Increase by max 10%
								new_issued = RDT_test + random.randint(0, int(RDT_test*0.1))
								if new_issued > stock_on_hand:
									print('issued > stock on hand ' + str(new_issued) + ' > ' + str(stock_on_hand))
									# Increase received
									increment_in_received = (new_issued - stock_on_hand + random.randint(1, 5))
									stock_on_hand += increment_in_received
									new_df.at[index, received_column_name] = new_df.iloc[index][received_column_name] + increment_in_received
								new_df.at[index, column] = new_issued
						stock_on_hand = stock_on_hand - new_df.iloc[index][column]
					elif 'on hand' in column:
						if stock_on_hand < 0:
							print('stock on hand negative')
							while stock_on_hand < 0:
								increment_in_received = ((-1)*stock_on_hand + random.randint(1,5))
								stock_on_hand += increment_in_received
								new_df.at[index, received_column_name] = new_df.iloc[index][received_column_name] + increment_in_received
							print('new stock at hand : ' + str(stock_on_hand))
						if not is_stock_out and random.choice([1,2,3,4,5,6,7,8,9,10]) == 5:
							is_stock_out = True
							# Determine how many periods of stock out
							stock_out_periods = random.randint(1, max_stock_out_periods)
							print('stock out periods = ' + str(stock_out_periods))
						if stock_out_periods > 0:
							print('period ' + str(stock_out_periods))
							new_df.at[index, column_issued_name] += stock_on_hand
							stock_on_hand = 0
							stock_out_periods -= 1
						new_df.at[index, column] = stock_on_hand
					elif 'out day' in column:
						new_df[new_df < 0] = 0
						if stock_on_hand == 0:
							print('stock out at ' + str(new_df.iloc[index]['period']))
							new_df.at[index, column] = random.randint(1, get_number_days_from_monthly_period(str(int(new_df.iloc[index]['period']))))
							number_facilities_stock_days[new_df.at[index, 'period']] += 1
						else:
							new_df.at[index, column] = 0
			new_df['period'] = new_df['period'].astype(int).astype(str)
			new_df = new_df[new_df[column_issued_name].notna()]
			#result = new_df.to_csv(r'./' + stock_prefix + '.csv', index=None, header=True)
			new_unpivoted = new_df.melt(id_vars=['period'], var_name='dataelement', value_name='value')
			new_unpivoted = new_unpivoted.replace({"dataelement": de})
			new_unpivoted['orgunit'] = ou
			new_unpivoted['value'] = new_unpivoted['value'].astype(int)

			if df_result.empty:
				df_result = new_unpivoted
			else:
				df_result = pd.concat([df_result, new_unpivoted])

		result = df_result.to_csv(r'./' + stock_prefix + '.csv', index=None, header=True)


if __name__ == '__main__':
	main()
