import re
import openpyxl
from openpyxl.styles import PatternFill, Font, Alignment
from openpyxl.formatting.rule import CellIsRule
from dhis2_env import api_from_env, credentials

# ==========================================
# 1. RAW DATA GRIDS
# ==========================================
bmi_raw_text = """
40 42 44 47 50 49 51 54 57 60 33 34 35 36 37 43 44 46 47 48 >=180
33 35 38 40 42 42 44 47 49 52 29 29 30 31 32 38 39 40 41 42 160-179
70-74 28 30 32 34 36 35 37 40 42 45 25 25 26 27 28 33 34 35 36 37 140-159
23 25 26 28 30 29 31 33 36 38 21 22 23 23 24 29 30 30 31 32 120-139
19 20 22 23 25 24 26 28 30 32 18 19 19 20 21 25 26 26 27 28 <120
31 34 36 39 42 42 45 48 51 55 27 28 29 30 31 38 39 41 42 43 >=180
26 28 30 32 35 35 37 40 43 47 23 24 24 25 26 33 34 35 36 37 160-179
65-69 21 22 24 27 29 28 31 33 36 39 19 20 21 21 22 28 29 30 31 32 140-159
17 18 20 22 23 23 25 27 30 32 16 17 17 18 19 24 25 25 26 27 120-139
13 15 16 17 19 19 20 22 24 26 13 14 15 15 16 20 21 22 22 23 <120
25 27 30 32 36 35 39 42 46 50 22 23 23 24 25 34 35 36 37 39 >=180
19 21 24 26 29 28 31 34 38 41 18 19 19 20 21 28 29 30 32 33 160-179
60-64 15 17 19 21 23 23 25 28 31 34 15 15 16 17 17 23 24 25 27 28 140-159
12 13 15 16 18 18 20 22 25 27 12 13 13 14 14 19 20 21 22 23 120-139
9 10 12 13 14 14 16 18 20 22 10 10 11 11 12 16 17 18 18 19 <120
19 21 24 27 30 30 33 37 41 45 18 18 19 20 21 29 31 32 33 35 >=180
15 16 18 21 23 23 26 29 33 37 14 15 15 16 17 24 25 26 28 29 160-179
55-59 11 13 14 16 18 18 20 23 26 29 11 12 12 13 14 20 21 22 23 24 140-159
9 10 11 12 14 14 16 18 20 23 9 9 10 10 11 16 17 18 18 19 120-139
6 7 8 9 11 11 12 14 16 18 7 8 8 8 9 13 13 14 15 16 <120
15 17 19 22 25 25 28 32 36 41 14 15 15 16 17 26 27 28 29 31 >=180
11 13 14 16 19 19 22 25 28 32 11 12 12 13 13 20 21 23 24 25 160-179
50-54 8 9 11 12 14 14 16 19 22 25 9 9 10 10 11 16 17 18 19 20 140-159
6 7 8 9 11 11 12 14 17 19 7 7 7 8 8 13 14 14 15 16 120-139
4 5 6 7 8 8 9 11 13 15 5 6 6 6 7 10 11 11 12 13 <120
11 13 15 17 20 21 24 27 32 37 11 12 12 13 14 22 23 25 26 28 >=180
8 10 11 13 15 15 18 21 24 28 9 9 10 10 11 17 18 19 21 22 160-179
45-49 6 7 8 9 11 11 13 15 18 21 7 7 7 8 8 13 14 15 16 17 140-159
4 5 6 7 8 8 10 11 14 16 5 5 6 6 6 10 11 12 13 14 120-139
3 4 4 5 6 6 7 8 10 12 4 4 4 5 5 8 9 9 10 11 <120
9 10 12 14 17 17 20 24 28 33 9 9 10 11 11 19 20 22 23 24 >=180
6 7 8 10 12 12 15 17 21 25 7 7 7 8 8 15 16 17 18 19 160-179
40-44 4 5 6 7 9 9 11 13 15 18 5 5 6 6 6 11 12 13 14 15 140-159
3 4 4 5 6 6 8 9 11 13 4 4 4 5 5 8 9 10 10 11 120-139
2 3 3 4 4 4 5 7 8 10 3 3 3 3 4 6 7 7 8 9 <120
"""

lab_raw_text = """
37 40 43 46 50 45 48 52 56 60 29 31 32 33 34 39 40 41 43 45 >=180 47 51 55 59 63 57 61 65 69 73 41 42 44 45 47 52 54 55 57 59 >=180
31 33 36 40 43 38 41 45 48 52 26 27 28 29 30 34 35 36 38 39 160-179 40 44 48 52 56 49 53 57 61 66 36 37 39 40 42 46 48 50 51 53 160-179
70-74 26 28 31 34 37 32 35 38 42 45 22 23 24 25 26 30 31 32 33 35 140-159 70-74 34 37 41 44 48 42 46 50 54 58 31 33 34 35 37 41 42 44 46 47 140-159
21 23 26 28 31 27 29 32 35 39 19 20 21 22 23 26 27 28 29 30 120-139 29 31 35 38 42 36 39 43 47 51 27 28 30 31 32 36 37 39 40 42 120-139
18 19 21 24 26 22 24 27 30 33 16 17 18 19 20 22 23 24 25 26 <120 24 26 29 32 35 30 33 36 40 44 24 25 26 27 28 31 33 34 35 37 <120
29 32 35 38 42 38 41 45 49 54 23 25 26 27 29 33 35 36 38 40 >=180 40 44 48 52 57 51 56 60 65 70 35 36 38 40 42 48 50 52 54 56 >=180
24 26 29 32 35 31 35 38 42 46 20 21 22 23 24 29 30 31 33 35 160-179 34 37 40 45 49 43 48 52 56 61 30 31 33 35 36 42 43 45 48 50 160-179
65-69 19 21 24 26 29 26 29 32 35 39 17 18 19 20 21 24 26 27 28 30 140-159 65-69 28 31 34 37 41 36 40 44 48 53 26 27 28 30 31 36 38 40 42 44 140-159
16 17 19 22 24 21 23 26 29 32 14 15 16 17 18 21 22 23 24 26 120-139 23 25 28 31 35 30 33 37 41 46 22 23 24 26 27 31 33 34 36 38 120-139
13 14 16 18 20 17 19 21 24 27 12 13 13 14 15 18 19 20 21 22 <120 18 21 23 26 29 25 28 31 34 38 19 20 21 22 23 27 28 30 31 33 <120
23 25 28 31 35 32 35 39 43 48 19 20 21 22 23 28 30 32 34 36 >=180 34 38 42 46 51 46 51 55 61 66 29 31 33 35 37 43 46 48 51 53 >=180
18 20 23 25 29 26 29 32 36 40 15 16 17 18 20 24 25 27 29 30 160-179 28 31 34 38 42 38 42 47 52 57 25 26 28 30 31 37 39 42 44 47 160-179
60-64 14 16 18 20 23 21 23 26 29 33 13 14 14 15 16 20 21 23 24 26 140-159 60-64 22 25 28 31 35 31 35 39 43 48 21 22 23 25 27 32 34 36 38 40 140-159
11 13 14 16 19 16 19 21 24 27 11 11 12 13 14 17 18 19 20 22 120-139 18 20 22 25 29 25 28 32 36 41 17 18 20 21 23 27 29 30 32 35 120-139
9 10 11 13 15 13 15 17 19 22 9 9 10 11 11 14 15 16 17 18 <120 14 16 18 21 23 20 23 26 30 34 14 15 16 18 19 23 24 26 28 30 <120
18 20 22 25 29 27 30 34 38 42 15 16 17 18 19 24 26 28 30 32 >=180 29 32 36 40 45 42 46 51 56 62 25 26 28 30 32 40 42 45 47 51 >=180
14 16 18 20 23 21 24 27 30 35 12 13 14 15 16 20 21 23 25 27 160-179 23 25 29 32 37 34 38 42 47 53 20 22 23 25 27 33 36 38 41 43 160-179
55-59 11 12 14 16 18 16 19 21 24 28 10 10 11 12 13 16 18 19 20 22 140-159 55-59 18 20 23 26 30 27 30 34 39 44 17 18 19 21 23 28 30 32 34 37 140-159
8 9 11 12 14 13 15 17 19 22 8 8 9 10 11 13 14 16 17 18 120-139 14 16 18 21 24 21 24 27 31 36 14 15 16 17 19 23 25 27 29 31 120-139
6 7 8 10 11 10 11 13 15 18 6 7 7 8 9 11 12 13 14 15 <120 11 12 14 16 19 17 19 22 25 29 11 12 13 14 15 19 21 22 24 26 <120
14 16 18 20 23 22 25 29 33 37 11 12 13 14 16 20 22 24 26 28 >=180 24 27 30 34 39 37 42 47 52 58 21 22 24 26 28 36 38 41 44 48 >=180
10 12 14 16 18 17 19 22 26 30 9 10 11 12 13 17 18 19 21 23 160-179 18 21 24 27 31 29 33 38 43 48 17 18 20 21 23 30 32 35 37 40 160-179
50-54 8 9 10 12 14 13 15 17 20 24 7 8 9 9 10 13 14 16 17 19 140-159 50-54 14 16 18 21 25 23 26 30 34 40 13 15 16 17 19 24 26 29 31 34 140-159
6 7 8 9 11 10 11 13 16 18 6 6 7 7 8 11 12 13 14 15 120-139 11 12 14 16 19 18 20 23 27 32 11 12 13 14 15 20 22 24 26 28 120-139
4 5 6 7 8 7 9 10 12 14 5 5 5 6 7 9 9 10 11 13 <120 8 9 11 13 15 13 16 18 21 25 9 9 10 11 13 16 18 19 21 23 <120
11 12 14 16 19 19 21 24 28 33 9 10 11 12 13 17 19 21 23 25 >=180 20 23 26 30 34 33 38 42 48 55 17 19 21 23 25 32 35 38 41 45 >=180
8 9 10 12 14 14 16 18 22 25 7 8 8 9 10 14 15 16 18 20 160-179 15 17 20 23 27 26 29 33 38 44 14 15 16 18 20 26 29 31 34 38 160-179
45-49 6 7 8 9 11 10 12 14 17 20 5 6 7 7 8 11 12 13 15 16 140-159 45-49 11 13 15 17 21 19 22 26 30 36 11 12 13 14 16 21 23 26 28 31 140-159
4 5 6 7 8 8 9 11 13 15 4 5 5 6 6 9 9 10 12 13 120-139 8 10 11 13 16 15 17 20 24 28 8 9 10 11 13 17 19 21 23 26 120-139
3 4 4 5 6 6 7 8 10 12 3 4 4 4 5 7 7 8 9 10 <120 6 7 8 10 12 11 13 15 18 22 7 7 8 9 10 14 15 17 19 21 <120
8 9 11 13 15 15 18 20 24 28 7 8 8 9 10 15 16 18 20 22 >=180 17 19 22 25 30 30 34 39 44 51 15 16 18 19 21 29 32 35 39 42 >=180
6 7 8 9 11 11 13 15 18 22 5 6 6 7 8 11 13 14 16 17 160-179 12 14 16 19 22 22 26 30 35 41 11 12 14 15 17 23 26 28 31 35 160-179
40-44 4 5 6 7 8 8 9 11 14 16 4 4 5 6 6 9 10 11 12 14 140-159 40-44 9 10 12 14 17 16 19 22 27 32 9 10 11 12 13 18 20 23 25 28 140-159
3 4 4 5 6 6 7 8 10 12 3 3 4 4 5 7 8 9 10 11 120-139 6 7 9 10 13 12 14 17 20 25 7 7 8 9 10 14 16 18 20 23 120-139
2 3 3 4 5 4 5 6 8 9 2 3 3 3 4 5 6 7 8 9 <120 5 5 6 8 10 9 10 13 15 19 5 6 6 7 8 11 13 14 16 19 <120
"""

# ==========================================
# 2. PARSE THE TEXT BLOCKS
# ==========================================
valid_bmi_lines = []
for line in bmi_raw_text.strip().split('\n'):
    clean_line = re.sub(r'\b\d{2}-\d{2}\b', '', line).strip()
    parts = clean_line.split()
    if len(parts) >= 21:
        valid_bmi_lines.append(parts)

valid_lab_lines = []
for line in lab_raw_text.strip().split('\n'):
    clean_line = re.sub(r'\b\d{2}-\d{2}\b', '', line).strip()
    parts = clean_line.split()
    if len(parts) >= 42:
        valid_lab_lines.append(parts)

score_cols = ['D', 'E', 'F', 'G', 'H', 'I', 'J', 'K', 'L', 'M', 'O', 'P', 'Q', 'R', 'S', 'T', 'U', 'V', 'W', 'X']
ages = ['70-74', '65-69', '60-64', '55-59', '50-54', '45-49', '40-44']
sbp_levels = ['>=180', '160-179', '140-159', '120-139', '<120']


# ==========================================
# 3. EXCEL SHEET BUILDER
# ==========================================
def build_and_populate_sheet(ws, sheet_type):
    is_lab_based = sheet_type in ['lab_no_diab', 'lab_diab']

    # Setup Headers (Sex and Smoking Status)
    ws.merge_cells('D1:M1')
    ws['D1'] = 'Men'
    ws['D1'].alignment = Alignment(horizontal='center')
    ws['D1'].font = Font(bold=True)

    ws.merge_cells('O1:X1')
    ws['O1'] = 'Women'
    ws['O1'].alignment = Alignment(horizontal='center')
    ws['O1'].font = Font(bold=True)

    for start_col, title in [('D', 'Non-smoker'), ('I', 'Smoker'), ('O', 'Non-smoker'), ('T', 'Smoker')]:
        ws.merge_cells(f'{start_col}2:{chr(ord(start_col) + 4)}2')
        ws[f'{start_col}2'] = title
        ws[f'{start_col}2'].alignment = Alignment(horizontal='center')

    ws['A4'] = 'Age'
    ws['A4'].font = Font(bold=True)
    ws['B4'] = 'SBP (mmHg)'
    ws['B4'].font = Font(bold=True)

    row_idx = 5
    lines_data = valid_bmi_lines if sheet_type == 'non_lab' else valid_lab_lines

    for i, parts in enumerate(lines_data):
        if i % 5 == 0:
            age_group_index = i // 5
            ws.merge_cells(f'A{row_idx}:A{row_idx + 4}')
            ws[f'A{row_idx}'] = ages[age_group_index]
            ws[f'A{row_idx}'].alignment = Alignment(vertical='center', horizontal='center')

        ws[f'B{row_idx}'] = sbp_levels[i % 5]

        if sheet_type == 'non_lab':
            scores = parts[:20]
        elif sheet_type == 'lab_no_diab':
            scores = parts[:20]
        elif sheet_type == 'lab_diab':
            scores = parts[21:41]

        for col, score_val in zip(score_cols, scores):
            ws[f'{col}{row_idx}'] = int(score_val)

        row_idx += 1

    footer_row = row_idx
    footer_label = 'Total Cholesterol (mmol/L)' if is_lab_based else 'BMI (kg/m²)'
    ws.merge_cells(f'A{footer_row}:B{footer_row}')
    ws[f'A{footer_row}'] = footer_label
    ws[f'A{footer_row}'].font = Font(bold=True)

    footer_values = ['<4', '4-4.9', '5-5.9', '6-6.9', '≥7'] if is_lab_based else ['<20', '20-24', '25-29', '30-34',
                                                                                  '≥35']

    for i, col in enumerate(score_cols):
        ws[f'{col}{footer_row}'] = footer_values[i % 5]
        ws[f'{col}{footer_row}'].alignment = Alignment(horizontal='center')

    # Apply Updated WHO Conditional Formatting Rules
    green_fill = PatternFill(start_color='00FF00', end_color='00FF00', fill_type='solid')  # < 5
    yellow_fill = PatternFill(start_color='FFFF00', end_color='FFFF00', fill_type='solid')  # 5 to 9.99
    orange_fill = PatternFill(start_color='FFA500', end_color='FFA500', fill_type='solid')  # 10 to 19.99
    red_fill = PatternFill(start_color='FF0000', end_color='FF0000', fill_type='solid')  # 20 to 29.99
    dark_red_fill = PatternFill(start_color='8B0000', end_color='8B0000', fill_type='solid')  # >= 30
    white_font = Font(color='FFFFFF')  # Added white text color for dark red background

    data_range = f'D5:X{row_idx - 1}'
    ws.conditional_formatting.add(data_range,
                                  CellIsRule(operator='lessThan', formula=['5'], stopIfTrue=True, fill=green_fill))
    ws.conditional_formatting.add(data_range, CellIsRule(operator='between', formula=['5', '9.99'], stopIfTrue=True,
                                                         fill=yellow_fill))
    ws.conditional_formatting.add(data_range, CellIsRule(operator='between', formula=['10', '19.99'], stopIfTrue=True,
                                                         fill=orange_fill))
    ws.conditional_formatting.add(data_range, CellIsRule(operator='between', formula=['20', '29.99'], stopIfTrue=True,
                                                         fill=red_fill))
    # Passed the 'font' parameter into the >= 30 condition here:
    ws.conditional_formatting.add(data_range, CellIsRule(operator='greaterThanOrEqual', formula=['30'], stopIfTrue=True,
                                                         fill=dark_red_fill, font=white_font))


# ==========================================
# 4. EXECUTOR
# ==========================================
def generate_populated_spreadsheet():
    wb = openpyxl.Workbook()

    # 1. Non-lab score
    ws1 = wb.active
    ws1.title = "Non-lab score"
    build_and_populate_sheet(ws1, 'non_lab')

    # 2. Lab-score no diabetes
    ws2 = wb.create_sheet(title="Lab-score no diabetes")
    build_and_populate_sheet(ws2, 'lab_no_diab')

    # 3. Lab-score with diabetes
    ws3 = wb.create_sheet(title="Lab-score with diabetes")
    build_and_populate_sheet(ws3, 'lab_diab')

    filename = "WHO_Risk_Charts_Populated.xlsx"
    wb.save(filename)
    print(f"File '{filename}' generated successfully with updated formatting.")


if __name__ == "__main__":
    generate_populated_spreadsheet()
exit(0)

# import pandas as pd
# import io
#
# # 1. Option Sets Content
# csv_option_sets = """id,name,code,valueType,[options-id],[options-name],[options-code],[options-sortOrder]
# OsAgeRange0,Tranche d’âge,,TEXT,OpAge0to11,0-11 mois,0-11m,1
# ,,,,OpAge12to23,12-23 mois,12-23m,2
# ,,,,OpAge24to59,24-59 mois,24-59m,3
# OsSexType00,Sexe,,TEXT,OpSexMale00,Masculin,MALE,1
# ,,,,OpSexFema00,Féminin,FEMALE,2
# OsVacStatus,Statut vaccinal,,TEXT,OpVacUpToDa,À jour,UP_TO_DATE,1
# ,,,,OpVacDelaye,Retard,DELAYED,2
# ,,,,OpVacUnderV,Sous vacciné,UNDER_VACCINATED,3
# ,,,,OpVacZeroDo,Zéro dose,ZERO_DOSE,4
# OsMuacColor,Périmètre brachial couleur,,TEXT,OpMuacRed00,Rouge,RED,1
# ,,,,OpMuacYello,Jaune,YELLOW,2
# ,,,,OpMuacGreen,Vert,GREEN,3
# OsAntigenLi,Antigènes (liste),,TEXT,OpAntBCG000,BCG,BCG,1
# ,,,,OpAntPolio0,Polio,POLIO,2
# ,,,,OpAntPenta0,Penta,PENTA,3
# OsDoseList0,Doses (liste),,TEXT,OpDose00000,Dose 0,DOSE_0,0
# ,,,,OpDose01000,Dose 1,DOSE_1,1
# ,,,,OpDose02000,Dose 2,DOSE_2,2
# ,,,,OpDose03000,Dose 3,DOSE_3,3"""
#
# # 2. Data Elements Content
# csv_data_elements = """id,name,shortName,code,formName,description,domainType,aggregationType,valueType,optionSet-id,optionSet-name,categoryCombo-id,categoryCombo-name,zeroIsSignificant
# DeAgeChild0,Âge de l’enfant,Âge enfant,AGE_ENFANT,Âge de l’enfant,,TRACKER,NONE,TEXT,OsAgeRange0,Tranche d’âge,bjDvmb4bfuf,default,False
# DeSexChild0,Sexe de l’enfant,Sexe,SEXE_ENFANT,Sexe de l’enfant,,TRACKER,NONE,TEXT,OsSexType00,Sexe,bjDvmb4bfuf,default,False
# DeVacStatus,Statut vaccinal,Statut vac,STATUT_VAC,Statut vaccinal,,TRACKER,NONE,TEXT,OsVacStatus,Statut vaccinal,bjDvmb4bfuf,default,False
# DeMuacColor,État nutritionnel (Périmètre brachial),Périmètre brachial,MUAC_COLOR,Périmètre brachial couleur,,TRACKER,NONE,TEXT,OsMuacColor,Périmètre brachial couleur,bjDvmb4bfuf,default,False
# DeAntigens0,Antigènes,Antigènes,ANTIGENES,Antigènes,,TRACKER,NONE,TEXT,OsAntigenLi,Antigènes (liste),bjDvmb4bfuf,default,False
# DeDosesList,Doses,Doses,DOSES,Doses,,TRACKER,NONE,TEXT,OsDoseList0,Doses (liste),bjDvmb4bfuf,default,False
# DeVialRetrn,Nombre de flacons restitués par antigène,Flacons restitués,FLACONS_RESTITUES,Nombre de flacons restitués par antigène,AGGREGATE,SUM,INTEGER_ZERO_OR_POSITIVE,,,bjDvmb4bfuf,default,False
# DeVialLost0,Nombre de flacons perdus par antigène,Flacons perdus,FLACONS_PERDUS,Nombre de flacons perdus par antigène,AGGREGATE,SUM,INTEGER_ZERO_OR_POSITIVE,,,bjDvmb4bfuf,default,False
# DeComments0,Commentaires,Commentaires,COMMENTS,Commentaires (note vocale),AGGREGATE,NONE,LONG_TEXT,,,bjDvmb4bfuf,default,False"""
#
# # 3. Program Rule Variables Content
# csv_pr_variables = """id,name,program-id,program-name,programRuleVariableSourceType,valueType,useCodeForOptionSet,dataElement-id,dataElement-name,trackedEntityAttribute-id,trackedEntityAttribute-name
# PrvAntigene,var_antigene,PrgVaccin01,Programme Vaccination,DATAELEMENT_CURRENT_EVENT,TEXT,true,DeAntigens0,Antigènes,,"""
#
# # 4. Program Rules Content
# csv_program_rules = """id,name,description,priority,condition,program-id,program-name
# PrRuleBCG00,Filtrer Doses pour BCG,Cache les doses 1, 2 et 3 si BCG est sélectionné,1,#{var_antigene} == 'BCG',PrgVaccin01,Programme Vaccination
# PrRulePenta,Filtrer Doses pour Penta,Cache la dose 0 si Penta est sélectionné,1,#{var_antigene} == 'PENTA',PrgVaccin01,Programme Vaccination"""
#
# # 5. Program Rule Actions Content
# csv_pr_actions = """id,programRuleActionType,evaluationTime,location,programRule-id,programRule-name,data,content,dataElement-id,dataElement-name,option-id,option-name,optionGroup-id,optionGroup-name
# ActBcgHide1,HIDEOPTION,,,PrRuleBCG00,Filtrer Doses pour BCG,,,,DeDosesList,Doses,OpDose01000,Dose 1,,
# ActBcgHide2,HIDEOPTION,,,PrRuleBCG00,Filtrer Doses pour BCG,,,,DeDosesList,Doses,OpDose02000,Dose 2,,
# ActBcgHide3,HIDEOPTION,,,PrRuleBCG00,Filtrer Doses pour BCG,,,,DeDosesList,Doses,OpDose03000,Dose 3,,
# ActPenHide0,HIDEOPTION,,,PrRulePenta,Filtrer Doses pour Penta,,,,DeDosesList,Doses,OpDose00000,Dose 0,,"""
#
# # Dictionary mapping sheet names to their CSV content
# data_map = {
#     "Option Sets": csv_option_sets,
#     "Data Elements": csv_data_elements,
#     "Program Rule Variables": csv_pr_variables,
#     "Program Rules": csv_program_rules,
#     "Program Rule Actions": csv_pr_actions
# }
#
# # Write to Excel
# file_path = "DHIS2_Metadata_Flat_File.xlsx"
# with pd.ExcelWriter(file_path, engine='openpyxl') as writer:
#     for sheet_name, csv_content in data_map.items():
#         # keep_default_na=False prevents pandas from converting "NA" text to NaN objects
#         df = pd.read_csv(io.StringIO(csv_content), keep_default_na=False)
#         df.to_excel(writer, sheet_name=sheet_name, index=False)
#
# print(f"File created: {file_path}")
#
# exit(0)

import re
import pandas as pd

# ==========================================
# 1. INPUT RAW TEXT
# ==========================================
bmi_raw_text = """
40 42 44 47 50 49 51 54 57 60 33 34 35 36 37 43 44 46 47 48 >=180
33 35 38 40 42 42 44 47 49 52 29 29 30 31 32 38 39 40 41 42 160-179
70-74 28 30 32 34 36 35 37 40 42 45 25 25 26 27 28 33 34 35 36 37 140-159
23 25 26 28 30 29 31 33 36 38 21 22 23 23 24 29 30 30 31 32 120-139
19 20 22 23 25 24 26 28 30 32 18 19 19 20 21 25 26 26 27 28 <120
31 34 36 39 42 42 45 48 51 55 27 28 29 30 31 38 39 41 42 43 >=180
26 28 30 32 35 35 37 40 43 47 23 24 24 25 26 33 34 35 36 37 160-179
65-69 21 22 24 27 29 28 31 33 36 39 19 20 21 21 22 28 29 30 31 32 140-159
17 18 20 22 23 23 25 27 30 32 16 17 17 18 19 24 25 25 26 27 120-139
13 15 16 17 19 19 20 22 24 26 13 14 15 15 16 20 21 22 22 23 <120
25 27 30 32 36 35 39 42 46 50 22 23 23 24 25 34 35 36 37 39 >=180
19 21 24 26 29 28 31 34 38 41 18 19 19 20 21 28 29 30 32 33 160-179
60-64 15 17 19 21 23 23 25 28 31 34 15 15 16 17 17 23 24 25 27 28 140-159
12 13 15 16 18 18 20 22 25 27 12 13 13 14 14 19 20 21 22 23 120-139
9 10 12 13 14 14 16 18 20 22 10 10 11 11 12 16 17 18 18 19 <120
19 21 24 27 30 30 33 37 41 45 18 18 19 20 21 29 31 32 33 35 >=180
15 16 18 21 23 23 26 29 33 37 14 15 15 16 17 24 25 26 28 29 160-179
55-59 11 13 14 16 18 18 20 23 26 29 11 12 12 13 14 20 21 22 23 24 140-159
9 10 11 12 14 14 16 18 20 23 9 9 10 10 11 16 17 18 18 19 120-139
6 7 8 9 11 11 12 14 16 18 7 8 8 8 9 13 13 14 15 16 <120
15 17 19 22 25 25 28 32 36 41 14 15 15 16 17 26 27 28 29 31 >=180
11 13 14 16 19 19 22 25 28 32 11 12 12 13 13 20 21 23 24 25 160-179
50-54 8 9 11 12 14 14 16 19 22 25 9 9 10 10 11 16 17 18 19 20 140-159
6 7 8 9 11 11 12 14 17 19 7 7 7 8 8 13 14 14 15 16 120-139
4 5 6 7 8 8 9 11 13 15 5 6 6 6 7 10 11 11 12 13 <120
11 13 15 17 20 21 24 27 32 37 11 12 12 13 14 22 23 25 26 28 >=180
8 10 11 13 15 15 18 21 24 28 9 9 10 10 11 17 18 19 21 22 160-179
45-49 6 7 8 9 11 11 13 15 18 21 7 7 7 8 8 13 14 15 16 17 140-159
4 5 6 7 8 8 10 11 14 16 5 5 6 6 6 10 11 12 13 14 120-139
3 4 4 5 6 6 7 8 10 12 4 4 4 5 5 8 9 9 10 11 <120
9 10 12 14 17 17 20 24 28 33 9 9 10 11 11 19 20 22 23 24 >=180
6 7 8 10 12 12 15 17 21 25 7 7 7 8 8 15 16 17 18 19 160-179
40-44 4 5 6 7 9 9 11 13 15 18 5 5 6 6 6 11 12 13 14 15 140-159
3 4 4 5 6 6 8 9 11 13 4 4 4 5 5 8 9 10 10 11 120-139
2 3 3 4 4 4 5 7 8 10 3 3 3 3 4 6 7 7 8 9 <120
"""

lab_raw_text = """
37 40 43 46 50 45 48 52 56 60 29 31 32 33 34 39 40 41 43 45 >=180 47 51 55 59 63 57 61 65 69 73 41 42 44 45 47 52 54 55 57 59 >=180
31 33 36 40 43 38 41 45 48 52 26 27 28 29 30 34 35 36 38 39 160-179 40 44 48 52 56 49 53 57 61 66 36 37 39 40 42 46 48 50 51 53 160-179
70-74 26 28 31 34 37 32 35 38 42 45 22 23 24 25 26 30 31 32 33 35 140-159 70-74 34 37 41 44 48 42 46 50 54 58 31 33 34 35 37 41 42 44 46 47 140-159
21 23 26 28 31 27 29 32 35 39 19 20 21 22 23 26 27 28 29 30 120-139 29 31 35 38 42 36 39 43 47 51 27 28 30 31 32 36 37 39 40 42 120-139
18 19 21 24 26 22 24 27 30 33 16 17 18 19 20 22 23 24 25 26 <120 24 26 29 32 35 30 33 36 40 44 24 25 26 27 28 31 33 34 35 37 <120
29 32 35 38 42 38 41 45 49 54 23 25 26 27 29 33 35 36 38 40 >=180 40 44 48 52 57 51 56 60 65 70 35 36 38 40 42 48 50 52 54 56 >=180
24 26 29 32 35 31 35 38 42 46 20 21 22 23 24 29 30 31 33 35 160-179 34 37 40 45 49 43 48 52 56 61 30 31 33 35 36 42 43 45 48 50 160-179
65-69 19 21 24 26 29 26 29 32 35 39 17 18 19 20 21 24 26 27 28 30 140-159 65-69 28 31 34 37 41 36 40 44 48 53 26 27 28 30 31 36 38 40 42 44 140-159
16 17 19 22 24 21 23 26 29 32 14 15 16 17 18 21 22 23 24 26 120-139 23 25 28 31 35 30 33 37 41 46 22 23 24 26 27 31 33 34 36 38 120-139
13 14 16 18 20 17 19 21 24 27 12 13 13 14 15 18 19 20 21 22 <120 18 21 23 26 29 25 28 31 34 38 19 20 21 22 23 27 28 30 31 33 <120
23 25 28 31 35 32 35 39 43 48 19 20 21 22 23 28 30 32 34 36 >=180 34 38 42 46 51 46 51 55 61 66 29 31 33 35 37 43 46 48 51 53 >=180
18 20 23 25 29 26 29 32 36 40 15 16 17 18 20 24 25 27 29 30 160-179 28 31 34 38 42 38 42 47 52 57 25 26 28 30 31 37 39 42 44 47 160-179
60-64 14 16 18 20 23 21 23 26 29 33 13 14 14 15 16 20 21 23 24 26 140-159 60-64 22 25 28 31 35 31 35 39 43 48 21 22 23 25 27 32 34 36 38 40 140-159
11 13 14 16 19 16 19 21 24 27 11 11 12 13 14 17 18 19 20 22 120-139 18 20 22 25 29 25 28 32 36 41 17 18 20 21 23 27 29 30 32 35 120-139
9 10 11 13 15 13 15 17 19 22 9 9 10 11 11 14 15 16 17 18 <120 14 16 18 21 23 20 23 26 30 34 14 15 16 18 19 23 24 26 28 30 <120
18 20 22 25 29 27 30 34 38 42 15 16 17 18 19 24 26 28 30 32 >=180 29 32 36 40 45 42 46 51 56 62 25 26 28 30 32 40 42 45 47 51 >=180
14 16 18 20 23 21 24 27 30 35 12 13 14 15 16 20 21 23 25 27 160-179 23 25 29 32 37 34 38 42 47 53 20 22 23 25 27 33 36 38 41 43 160-179
55-59 11 12 14 16 18 16 19 21 24 28 10 10 11 12 13 16 18 19 20 22 140-159 55-59 18 20 23 26 30 27 30 34 39 44 17 18 19 21 23 28 30 32 34 37 140-159
8 9 11 12 14 13 15 17 19 22 8 8 9 10 11 13 14 16 17 18 120-139 14 16 18 21 24 21 24 27 31 36 14 15 16 17 19 23 25 27 29 31 120-139
6 7 8 10 11 10 11 13 15 18 6 7 7 8 9 11 12 13 14 15 <120 11 12 14 16 19 17 19 22 25 29 11 12 13 14 15 19 21 22 24 26 <120
14 16 18 20 23 22 25 29 33 37 11 12 13 14 16 20 22 24 26 28 >=180 24 27 30 34 39 37 42 47 52 58 21 22 24 26 28 36 38 41 44 48 >=180
10 12 14 16 18 17 19 22 26 30 9 10 11 12 13 17 18 19 21 23 160-179 18 21 24 27 31 29 33 38 43 48 17 18 20 21 23 30 32 35 37 40 160-179
50-54 8 9 10 12 14 13 15 17 20 24 7 8 9 9 10 13 14 16 17 19 140-159 50-54 14 16 18 21 25 23 26 30 34 40 13 15 16 17 19 24 26 29 31 34 140-159
6 7 8 9 11 10 11 13 16 18 6 6 7 7 8 11 12 13 14 15 120-139 11 12 14 16 19 18 20 23 27 32 11 12 13 14 15 20 22 24 26 28 120-139
4 5 6 7 8 7 9 10 12 14 5 5 5 6 7 9 9 10 11 13 <120 8 9 11 13 15 13 16 18 21 25 9 9 10 11 13 16 18 19 21 23 <120
11 12 14 16 19 19 21 24 28 33 9 10 11 12 13 17 19 21 23 25 >=180 20 23 26 30 34 33 38 42 48 55 17 19 21 23 25 32 35 38 41 45 >=180
8 9 10 12 14 14 16 18 22 25 7 8 8 9 10 14 15 16 18 20 160-179 15 17 20 23 27 26 29 33 38 44 14 15 16 18 20 26 29 31 34 38 160-179
45-49 6 7 8 9 11 10 12 14 17 20 5 6 7 7 8 11 12 13 15 16 140-159 45-49 11 13 15 17 21 19 22 26 30 36 11 12 13 14 16 21 23 26 28 31 140-159
4 5 6 7 8 8 9 11 13 15 4 5 5 6 6 9 9 10 12 13 120-139 8 10 11 13 16 15 17 20 24 28 8 9 10 11 13 17 19 21 23 26 120-139
3 4 4 5 6 6 7 8 10 12 3 4 4 4 5 7 7 8 9 10 <120 6 7 8 10 12 11 13 15 18 22 7 7 8 9 10 14 15 17 19 21 <120
8 9 11 13 15 15 18 20 24 28 7 8 8 9 10 15 16 18 20 22 >=180 17 19 22 25 30 30 34 39 44 51 15 16 18 19 21 29 32 35 39 42 >=180
6 7 8 9 11 11 13 15 18 22 5 6 6 7 8 11 13 14 16 17 160-179 12 14 16 19 22 22 26 30 35 41 11 12 14 15 17 23 26 28 31 35 160-179
40-44 4 5 6 7 8 8 9 11 14 16 4 4 5 6 6 9 10 11 12 14 140-159 40-44 9 10 12 14 17 16 19 22 27 32 9 10 11 12 13 18 20 23 25 28 140-159
3 4 4 5 6 6 7 8 10 12 3 3 4 4 5 7 8 9 10 11 120-139 6 7 9 10 13 12 14 17 20 25 7 7 8 9 10 14 16 18 20 23 120-139
2 3 3 4 5 4 5 6 8 9 2 3 3 3 4 5 6 7 8 9 <120 5 5 6 8 10 9 10 13 15 19 5 6 6 7 8 11 13 14 16 19 <120
"""

# ==========================================
# 2. PARSERS & UTILS
# ==========================================
bmi_ranges = ['< 20', '20 - 24', '25 - 29', '30 - 35', '>= 35']
chol_ranges = ['< 4', '4 - 4.9', '5 - 5.9', '6 - 6.9', '>= 7']
ages_list = ['70-74', '65-69', '60-64', '55-59', '50-54', '45-49', '40-44']

score_conditions = {}


def parse_range_continuous(range_str, variable):
    range_str = str(range_str).strip()
    if '>=' in range_str or '≥' in range_str:
        val = re.sub(r'[>=≥]', '', range_str).strip()
        return f"{variable} >= {val}"
    elif '>' in range_str:
        val = re.sub(r'[>]', '', range_str).strip()
        return f"{variable} > {val}"
    elif '<' in range_str:
        val = range_str.replace('<', '').strip()
        if variable == '#{bmi}':
            return f"{variable} > 0 && {variable} < {val}"
        return f"{variable} < {val}"
    elif '-' in range_str:
        low_str, high_str = range_str.split('-')
        low, high = float(low_str.strip()), float(high_str.strip())

        # Cholesterol ranges (e.g., 4-4.9 -> >=4 and <5)
        if variable == '#{cholesterol}':
            return f"{variable} >= {int(low)} && {variable} < {int(high + 1)}"

        # SBP, Age, BMI natively mapped boundaries
        if high % 10 == 4 or high % 10 == 9:
            return f"{variable} >= {int(low)} && {variable} < {int(high + 1)}"
        else:
            return f"{variable} >= {int(low)} && {variable} <= {int(high)}"
    return ""


def add_condition(score, condition_snippet):
    score = str(score)
    if score not in score_conditions:
        score_conditions[score] = []
    score_conditions[score].append(condition_snippet)


# ==========================================
# 3. PRE-PROCESS TO CLEAN LINES (Ignore inline ages)
# ==========================================
# By extracting exact valid rows, we bypass PDF column alignment issues
valid_bmi_lines = []
for line in bmi_raw_text.strip().split('\n'):
    clean_line = re.sub(r'\b\d{2}-\d{2}\b', '', line).strip()
    parts = clean_line.split()
    if len(parts) >= 21:
        valid_bmi_lines.append(parts[:21])

valid_lab_lines = []
for line in lab_raw_text.strip().split('\n'):
    clean_line = re.sub(r'\b\d{2}-\d{2}\b', '', line).strip()
    parts = clean_line.split()
    if len(parts) >= 42:
        valid_lab_lines.append(parts[:42])

# ==========================================
# 4. PARSE BMI (NON-LAB) DATA
# ==========================================
for row_idx, parts in enumerate(valid_bmi_lines):
    current_age = ages_list[row_idx // 5]  # Every 5 lines correspond to 1 age group

    sbp_label = parts[20]
    if '≥' in sbp_label or '>=' in sbp_label:
        sbp_label = ">= 180"
    elif '<' in sbp_label:
        sbp_label = "< 120"
    elif '-' in sbp_label and ' ' not in sbp_label:
        sbp_label = sbp_label.replace('-', ' - ')

    categories = [
        ('MALE', 'false', parts[0:5]),
        ('MALE', 'true', parts[5:10]),
        ('FEMALE', 'false', parts[10:15]),
        ('FEMALE', 'true', parts[15:20])
    ]

    for gender, smoking, risks in categories:
        for i, risk in enumerate(risks):
            g_rule = f"A{{sex}} == '{gender}'"
            s_rule = f"A{{smoking}} == {smoking}"
            a_rule = parse_range_continuous(current_age, 'A{age}')
            sbp_rule = parse_range_continuous(sbp_label, '#{systolicBP}')
            bmi_rule = parse_range_continuous(bmi_ranges[i], '#{bmi}')

            # FIXED: Only execute BMI logic if Lab (Cholesterol) is NOT provided
            snippet = f"(!d2:hasValue(#{{cholesterol}}) && d2:hasValue(#{{bmi}}) && {g_rule} && {s_rule} && {a_rule} && {sbp_rule} && {bmi_rule})"
            add_condition(risk, snippet)

# ==========================================
# 5. PARSE CHOLESTEROL (LAB) DATA
# ==========================================
for row_idx, parts in enumerate(valid_lab_lines):
    current_age = ages_list[row_idx // 5]

    sbp_label = parts[20]
    if '≥' in sbp_label or '>=' in sbp_label:
        sbp_label = ">= 180"
    elif '<' in sbp_label:
        sbp_label = "< 120"
    else:
        sbp_label = sbp_label.replace('-', ' - ')

    for diab_idx, has_diabetes in enumerate(['NO', 'YES']):
        offset = diab_idx * 21
        categories = [
            ('MALE', 'false', parts[offset: offset + 5]),
            ('MALE', 'true', parts[offset + 5: offset + 10]),
            ('FEMALE', 'false', parts[offset + 10: offset + 15]),
            ('FEMALE', 'true', parts[offset + 15: offset + 20])
        ]

        for gender, smoking, risks in categories:
            for i, risk in enumerate(risks):
                g_rule = f"A{{sex}} == '{gender}'"
                s_rule = f"A{{smoking}} == {smoking}"
                a_rule = parse_range_continuous(current_age, 'A{age}')
                sbp_rule = parse_range_continuous(sbp_label, '#{systolicBP}')
                chol_rule = parse_range_continuous(chol_ranges[i], '#{cholesterol}')

                # FIXED: Handling DHIS2 missing value evaluation for Diabetes Dropdown
                if has_diabetes == 'NO':
                    d_rule = "(!d2:hasValue(A{Patient_Diabetes}) || A{Patient_Diabetes} == 'NO')"
                else:
                    d_rule = "A{Patient_Diabetes} == 'YES'"

                snippet = f"(d2:hasValue(#{{cholesterol}}) && {g_rule} && {s_rule} && {d_rule} && {a_rule} && {sbp_rule} && {chol_rule})"
                add_condition(risk, snippet)

# ==========================================
# 6. ASSEMBLE DHIS2 DATAFRAMES
# ==========================================
prv_columns = ["id", "name", "program-id", "program-name", "programRuleVariableSourceType",
               "valueType", "useCodeForOptionSet", "programStage-id", "programStage-name",
               "dataElement-id", "dataElement-name", "trackedEntityAttribute-id", "trackedEntityAttribute-name"]
prv_data = [{
    "id": "",
    "name": "cholesterol",
    "program-id": "pMIglSEqPGS",
    "program-name": "Hypertension & Diabetes",
    "programRuleVariableSourceType": "DATAELEMENT_CURRENT_EVENT",
    "valueType": "NUMBER",
    "useCodeForOptionSet": "False",
    "programStage-id": "",
    "programStage-name": "",
    "dataElement-id": "",
    "dataElement-name": "HTN - Total Cholesterol",
    "trackedEntityAttribute-id": "",
    "trackedEntityAttribute-name": ""
}]
df_prv = pd.DataFrame(prv_data, columns=prv_columns)

pr_columns = ["id", "name", "priority", "condition", "program-id", "program-name"]
pra_columns = ["id", "programRuleActionType", "evaluationTime", "location", "programRule-id",
               "programRule-name", "data", "content", "dataElement-id", "dataElement-name",
               "trackedEntityAttribute-id", "trackedEntityAttribute-name", "programStageSection-id",
               "programStageSection-name", "programStage-id", "programStage-name", "option-id",
               "option-name", "optionGroup-id", "optionGroup-name"]

rules_list = []
actions_list = []

for score, snippets in score_conditions.items():
    rule_name = f"HTN - Risk = {score}"
    condition_str = " || ".join(snippets)

    rules_list.append({
        "id": "",
        "name": rule_name,
        "priority": 50,
        "condition": condition_str,
        "program-id": "pMIglSEqPGS",
        "program-name": "Hypertension & Diabetes"
    })

    actions_list.append({
        "id": "",
        "programRuleActionType": "ASSIGN",
        "evaluationTime": "",
        "location": "",
        "programRule-id": "",
        "programRule-name": rule_name,
        "data": score,
        "content": "#{riskScore}",
        "dataElement-id": "qwCWN0aLHAi",
        "dataElement-name": "HTN - Risk Score (number)",
        "trackedEntityAttribute-id": "",
        "trackedEntityAttribute-name": "",
        "programStageSection-id": "",
        "programStageSection-name": "",
        "programStage-id": "",
        "programStage-name": "",
        "option-id": "",
        "option-name": "",
        "optionGroup-id": "",
        "optionGroup-name": ""
    })

    actions_list.append({
        "id": "",
        "programRuleActionType": "ASSIGN",
        "evaluationTime": "",
        "location": "",
        "programRule-id": "",
        "programRule-name": rule_name,
        "data": f"'{score}'",
        "content": "",
        "dataElement-id": "dzlxGoRUDt9",
        "dataElement-name": "HTN - Risk Score (text)",
        "trackedEntityAttribute-id": "",
        "trackedEntityAttribute-name": "",
        "programStageSection-id": "",
        "programStageSection-name": "",
        "programStage-id": "",
        "programStage-name": "",
        "option-id": "",
        "option-name": "",
        "optionGroup-id": "",
        "optionGroup-name": ""
    })

df_pr = pd.DataFrame(rules_list, columns=pr_columns)
df_pra = pd.DataFrame(actions_list, columns=pra_columns)

with pd.ExcelWriter('Updated_Risk_Assessment_Rules.xlsx') as writer:
    df_prv.to_excel(writer, sheet_name='Program Rule Variables', index=False)
    df_pr.to_excel(writer, sheet_name='Program Rules', index=False)
    df_pra.to_excel(writer, sheet_name='Program Rule Actions', index=False)

print("Updated_Risk_Assessment_Rules.xlsx generated successfully.")
exit(0)








# import qrcode
# from IPython.display import display
#
# # Define the URL
# url = "https://dhis2-sandbox1.simple.org"
#
# # Generate the QR code
# qr = qrcode.QRCode(
#     version=1,
#     error_correction=qrcode.constants.ERROR_CORRECT_L,
#     box_size=10,
#     border=4,
# )
# qr.add_data(url)
# qr.make(fit=True)
#
# # Create an image from the QR code instance
# img = qr.make_image(fill_color="black", back_color="white")
#
# # Save and display the image
# img.save("dhis2_sandbox1_qr.png")
# display(img)
# exit(0)

import requests
import json

_ephi_url, USERNAME, PASSWORD = credentials('EPHI_STAGING')
BASE_URL = f"{_ephi_url}/api/reports/A5vbX2eM4z5"
HTML_FILE = "THDF_Report_Arrivals.html"

# 1. Download the report JSON
resp = requests.get(BASE_URL, auth=(USERNAME, PASSWORD))
resp.raise_for_status()

report = resp.json()

# 2. Load HTML file as string
with open(HTML_FILE, "r", encoding="utf-8") as f:
    html_content = f.read()

# 3. Replace designContent field
report["designContent"] = html_content

# 4. Upload updated report
put_resp = requests.put(
    BASE_URL,
    auth=(USERNAME, PASSWORD),
    headers={"Content-Type": "application/json"},
    data=json.dumps(report)
)

# 5. Print status and server response
print("Status:", put_resp.status_code)
print("Response:", put_resp.text)

exit(0)


from calendar import monthrange
from random import randint

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

#api_source = api_from_env('DHIS2_ROBOT', url='https://metadata.dev.dhis2.org/tracker_dev')

api_source = api_from_env('DHIS2_PLAY')

metadata_import_order = [
	'categoryOptionGroupSets', 'categoryOptionGroups',
	'categoryOptions', 'categories', 'categoryCombos', 'categoryOptionCombos',
	'legendSets',  # used in indicators, optionGroups, programIndicators and trackedEntityAttributes
	'optionGroups', 'options', 'optionSets',
	'sqlViews', 'reports', 'constants', 'documents', 'attributes',
	'dataEntryForms', 'sections', 'dataSets',  # Some programs, like HIV, have dataSets
	'dataElements', 'dataElementGroups',
	'validationNotificationTemplates', 'validationRules', 'validationRuleGroups',
	'jobConfigurations',
	'predictors', 'predictorGroups',
	'trackedEntityAttributes', 'trackedEntityTypes', 'trackedEntityInstanceFilters',
	'programNotificationTemplates',
	'programs', 'programSections',
	'programStageSections', 'programStages',
	'programIndicatorGroups', 'programIndicators',
	'organisationUnitGroupSets', 'organisationUnitGroups',  # Assuming this will only be found in indicators
	'indicatorTypes', 'indicatorGroupSets', 'indicators', 'indicatorGroups',
	'programRuleVariables', 'programRuleActions', 'programRules',
	'visualizations', 'maps', 'eventVisualizations', 'eventReports', 'eventCharts', 'dashboards',
	'users', 'userRoles', 'userGroups']

df = pd.DataFrame({}, columns=['metadata_type', 'owner_fields', 'extra_fields_in_*'])

for metadata_type in metadata_import_order:
	metadata = api_source.get(metadata_type, params={"fields":"*"}).json()[metadata_type]
	if len(metadata) > 0:
		all_keys = list(metadata[0].keys())
		all_keys.sort()
		metadata = api_source.get(metadata_type, params={"fields": ":owner"}).json()[metadata_type]
		owner_keys = list(metadata[0].keys())
		owner_keys.sort()
		common_keys = list()
		only_all = list()
		for key in all_keys:
			if key in owner_keys:
				common_keys.append(key)
			else:
				only_all.append(key)
		df.loc[len(df)] = [metadata_type, ', '.join(common_keys), ', '.join(only_all)]

df.to_csv(r'./summary_of_fields.csv', index=None, header=True)

exit(0)




api_source = api_from_env('DHIS2_TEST_REAL')

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

	#WHAT
	CC = api_source.get('categoryCombos', params={ "paging" : "false", "fields" : "id,name,categoryOptionCombos" }).json()['categoryCombos']
	CC = reindex(CC, 'id')
	COC = api_source.get('categoryOptionCombos', params={ "paging" : "false", "fields" : "id,name" }).json()['categoryOptionCombos']
	COC = reindex(COC, 'id')
	defaultCC = ''
	for catcomboUID in CC :
		if CC[catcomboUID]['name'] == 'default' :
			defaultCC = catcomboUID
			break
	if defaultCC == '' :
		logger.warning('Could not find default Category Combo')
	DE = api_source.get('dataElements', params={ "paging" : "false", "fields" : "id,name,categoryCombo,aggregationType,valueType" }).json()['dataElements']
	DE = reindex(DE, 'id')

	#WHEN
	startYear=2014
	endYear=2022
	logger.info('Using the time period from ' + str(startYear) + ' to ' + str(endYear))

	#WHERE
	OU = dict() # Indexed by level

	#Dataframe to store the
	result_df = pd.DataFrame({}, columns = ['Data Element', 'Category Option Combo', 'Organisation Unit'])
	result_df = None

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
		# if level == 1 :
		# 	#We are going to assume that there is ONLY one OU root at level 1
		# 	for uid in OU[level] :
		# 		if uid != 'KEGktGHjhYQ':
		# 			rootOU = uid
		# 	result_df = pd.concat([result_df,pd.DataFrame(columns=list('T'))])
		# 	result_df.rename(columns={'T' : levelName + '_NumberValues'}, inplace=True)	#Was getting ValueError: Plan shapes are not aligned, not sure why
		# else :
		# 	result_df = pd.concat([result_df,pd.DataFrame(columns=[levelName + '_NumberValues', levelName + '_NumberReporting'])])
		# 	logger.info('Total number of ' + levelName + ' (level=' + str(level) + '):' + str(len(OU[level])))

	#Data statistic values
	# Add extra columns - generic

	#result_df = pd.concat([result_df,pd.DataFrame(columns=['StartPeriod', 'EndPeriod'])])


	month_list = ['01', '02', '03', '04', '05', '06', '07', '08', '09', '10', '11', '12']
	quarter_list = ['Q1', 'Q2', 'Q3', 'Q4']
	total_number_of_periods = 0

	#start_time = time.time()
	periodType = 'Quarterly'
	dataSet_uids = ['eRB2lyfgCcC', 'cXVnVexZM2V', 'Q21U47uf0xo', 'AdOQpt5vQVw']
	dataSet_uids = ['cXVnVexZM2V', 'Q21U47uf0xo', 'AdOQpt5vQVw']
	dataset_DEs = {
		'eRB2lyfgCcC':['QoQpxqt8piI', 'TfymJ2qg7QJ', 'fLEJKIVG8am', 'JDXvAJcIigE', 'y2WVS42ezHn', 'Kt1EBm71tvP', 'qc2PfCyWn2i',
		'KiditKuJPVF', 'f6wJfTUDrZL', 'sUOjPE2IYt8', 'ugrJ8u4TiaV', 'E36DLjQOphV', 'ir3Ty7IS82J', 'h1WX9zIwQoy',
		'UA4I4jSANw0', 'YmkD0e5mIR7', 'yLsY6Vgzd5l', 'hGh6WXK5N42', 'E8r3II23f2B', 'e41VzkjbMg4', 'tUxHFLINdYb',
		'xRHAUBxeh9C', 'n4kKOXiSyD0', 'ytLhNuGpmXK', 'KCJnMX3HLUc', 'xSWeMt2CK3p', 'cbY2INEyc5L', 'ZbkzEtqfbjY',
		'nR4LV32DFHk', 'gscN2p8Xrdc', 'LQTCWC9nROm', 'Rvhw4UKEicH', 'l5n0ronRCdZ', 'vGj9uUExhTQ', 'rOSNh2omvQ5',
		'ni4j9thbm7x', 'iYN2ilHIpQU', 's8Gu6Gb0knq', 'dwjKnwdRqzB', 'V2VndOX039F', 'Fhb2FoN5QMx', 'NTD8iA8yCIj',
		'nsOiDRoI9Vs', 'EC2RzfOALyl', 'uQtf7nO4LQv', 'aJVrfNPJN3Q'],

		'cXVnVexZM2V': ['Mn5HrInzIpO', 'YaT75WKW8YS', 'Y3UqQmb1IWO', 'zNyg90A0Bbo',
		'gW8da1XgAl9', 'pUPo7JQHlcu', 'ulub6jeCRXS', 'A0ZTpQ8sQFq', 'ENetme4n6nB', 'HK6Vvp6rr0G', 'Ldgc4NY0WOf',
		'tqAJ97hlfAf', 'teZFQYGgP3K', 'AqtCtj8qZe4', 'XnAtePdPtQo', 'mYbsmXUzDxJ', 'sXZVO1u5wrV', 'BexRpFIr0ij',
		'hnR7Rv2HAhq', 'bhZy0JFIgVQ', 'r0ZHYkhAOfG', 'Z5IFhExpsBm', 'jXn90QwGmOA', 'hcOY9xpuKW8', 'c6qKHk0o79F',
		'b743YplpMjD', 'Qh2KBIuyQZe', 'oMk5qGNeGg3', 'tOSBxUUISQ9', 'LdA3m59ocpS', 'Le4rpjQMLmD', 'SmSfKTdnLrs',
		'yehmLOVCDYh', 'SkfSw1XRVbG', 'sqxfDGQ021c', 'lzuNOs0mW7W', 'fuohYjq5Srs', 'caTvqhxqQ6D', 'GOGZWcpQ4x1',
		'mXz1a9CQp0h', 'lXQeZm6DgUa', 'LEqlu8m9yyS', 'UWuUP5ScxsE', 'jC2t5y8lqbh', 'fkWBO1MTysS', 'SAHyqOOomyW'],

		'Q21U47uf0xo':['h8KiBz2888q'],

		'AdOQpt5vQVw':['ryvqfc7TOWr',
		'Y7l9wDz7Po3', 'ys4Y7YtewJ0', 'nZcX5DIl2Ez', 'b7ov1g6nlKz', 'dkKg2ztuiOA', 'Yby1SUc6Mi9', 'b8i8vDuewIM',
		'Jw3Dgi1rDCW', 'flDaFOI3oqP', 'tHE1a3ObExA', 'Uckrc7jFQjl', 'ljdwwN0i69V', 'YpLmUyG5tV3', 'cDguWAWk0Y6',
		's1qcHxvu6t0', 'nZcX5DIl2Ez', 'yDN8UHiCmGY', 'NVGTTWb3x2H', 'DguG9uGR1A4', 'b7ov1g6nlKz', 'dkKg2ztuiOA',
		'Yby1SUc6Mi9', 'b8i8vDuewIM', 'Jw3Dgi1rDCW', 'flDaFOI3oqP', 'yDN8UHiCmGY', 'tHE1a3ObExA', 'Uckrc7jFQjl',
		'ljdwwN0i69V', 'YpLmUyG5tV3', 'cDguWAWk0Y6', 's1qcHxvu6t0', 'b7ov1g6nlKz', 'dkKg2ztuiOA', 'Yby1SUc6Mi9',
		'b8i8vDuewIM', 'Jw3Dgi1rDCW', 'flDaFOI3oqP', 'b8i8vDuewIM', 'Jw3Dgi1rDCW', 'flDaFOI3oqP', 'b7ov1g6nlKz',
		'Yby1SUc6Mi9', 'dkKg2ztuiOA', 'Jw3Dgi1rDCW', 'flDaFOI3oqP', 'b8i8vDuewIM', 'MmP9JbJ8zNI', 'M5Tc1ik5osS',
		'd9VBG2HRwds', 'xzV3S3lqCh7', 'MmP9JbJ8zNI', 'AvvxpN3RUpM', 'CDIC5YQhNFJ', 'OrjwXESH8Zi']

	}

	dataSet_uids = ['TOZ3tvj9kfT', 'glPUtv9TeID']
	dataset_DEs = { 'TOZ3tvj9kfT':['D8Q6nNeQ7i3','Nt5y1WYCqv3'], 'glPUtv9TeID':['gf8hIKiGSwQ']}

	dataSet_uids = ['szn6Q0Z2s6o']
	dataset_DEs = { 'szn6Q0Z2s6o':['ocX8opThicv']}

	dataSets = api_source.get('dataSets', params={"paging": "false", "fields": "id,name,periodType", "filter":"id:in:["+','.join(dataSet_uids)+"]"}).json()['dataSets']
	dataSets = reindex(dataSets, 'id')


	for dataSet_uid in dataSet_uids:
		periodType = dataSets[dataSet_uid]['periodType']
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

		#Initilize variables
		for ou_level_2 in OU[2]:
			for pe in periods:

				logger.info(dataSets[dataSet_uid]['name'] + ' // ' + ou_level_2 + ' // PE = ' + pe)

				params = {
					'period': pe,
					'orgUnit': ou_level_2,
					'dataSet': dataSet_uid,
					'children': True
				}

				#Get data from source server
				retries = 0
				data = list()
				try:
					dataValueSets = api_source.get('dataValueSets', params=params).json()
				except Exception as e:
					if e.code == 409:
						logger.info("No data")
					else:
						logger.error(e)
				else:
					if 'dataValues' in dataValueSets:
						data = dataValueSets['dataValues']

				if len(data) > 0:
					data_df = pd.DataFrame.from_records(data)
					data_df = data_df[data_df["dataElement"].isin(dataset_DEs[dataSet_uid])]
					if result_df is None:
						result_df = data_df
					else:
						result_df = pd.concat([result_df, data_df], ignore_index=True)

		#new_row = dict()
		#new_row = {'DataSet':dsName, 'periodType':periodType, 'formType':formType, 'ouAssigned':orgUnitsAssignedToDS}


			# new_row['StoredBy'] = ", ".join(storedBy[deUID])
			# new_row['LastUpdated'] = lastUpdated[deUID]
			#Customization or Liberia
			# new_row['ODK_Form'] = ", ".join(odkForm[deUID])
			# new_row['SentBy'] = ", ".join(sentBy[deUID])
			#new_row['comment'] = ", ".join(comment[deUID])
			# result_df = result_df.append(new_row, ignore_index=True)

		export_csv = result_df.to_csv (r'./datavalues_'+dataSet_uid+'.csv', index = None, header=True)

if __name__ == '__main__':
	main()
