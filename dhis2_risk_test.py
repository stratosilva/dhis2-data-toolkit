import time
from datetime import datetime
import pandas as pd
from dataclasses import dataclass
from typing import List

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains
from dhis2_env import credentials


@dataclass
class TestCase:
    scenario: str
    dob: str
    sex: str
    smoker: str
    diabetes: str
    height: int
    weight: int
    sbp: int
    dbp: int
    chol: str
    expected_risk: str
    expected_class: str


class Dhis2RiskTester:
    def __init__(self, base_url: str, username: str, password: str):
        options = webdriver.ChromeOptions()
        # options.add_argument("--headless") # Uncomment to run invisibly
        self.driver = webdriver.Chrome(options=options)
        self.wait = WebDriverWait(self.driver, 10)
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password

    def login(self) -> None:
        print("Logging into DHIS2...")
        self.driver.get(f"{self.base_url}/dhis-web-commons/security/login.action")

        user_field = self.wait.until(EC.presence_of_element_located((By.ID, "j_username")))
        pass_field = self.driver.find_element(By.ID, "j_password")

        user_field.clear()
        user_field.send_keys(self.username)
        pass_field.clear()
        pass_field.send_keys(self.password)
        self.driver.find_element(By.ID, "submit").click()
        time.sleep(3)

    def smart_fill(self, keyword: str, value: str, field_type: str = "text"):
        """Intelligently detects field type, handles React state, and clears correctly."""
        if pd.isna(value) or value is None:
            value = ""
        else:
            value = str(value).strip()

        try:
            # REVERTED: Using the clean, case-sensitive XPath that works!
            base_xpath = f"//div[@data-test='form-field'][contains(., '{keyword}')]"
            wrapper = self.wait.until(EC.presence_of_element_located((By.XPATH, base_xpath)))
            self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", wrapper)
            time.sleep(0.2)

            if field_type == "choice":
                # FIXED: Looking for ANY combobox (*), not just inputs!
                comboboxes = wrapper.find_elements(By.XPATH, ".//*[@role='combobox']")
                radios = wrapper.find_elements(By.XPATH, ".//input[@type='radio']")

                if comboboxes:
                    # -- DROPDOWN LOGIC --
                    if value == "":
                        # Click the "X" clear zone to reset the dropdown
                        clear_zones = wrapper.find_elements(By.XPATH,
                                                            ".//*[contains(@class, 'clear-zone') or contains(@class, 'clear-icon') or contains(@data-test, 'clear')]")
                        if clear_zones:
                            self.driver.execute_script("arguments[0].click();", clear_zones[0])
                            time.sleep(0.2)
                    else:
                        combo = comboboxes[0]
                        # Click to open the menu and gain keyboard focus
                        self.driver.execute_script("arguments[0].click();", combo)
                        time.sleep(0.3)

                        # Apply your manual workaround: Supr/Delete -> Backspace -> Type -> Enter
                        actions = ActionChains(self.driver)
                        actions.send_keys(Keys.DELETE)
                        actions.send_keys(Keys.BACKSPACE)
                        actions.pause(0.2)
                        actions.send_keys(value)
                        actions.pause(1.0)  # Wait for React to filter 'Female' out
                        actions.send_keys(Keys.ENTER)
                        actions.perform()
                        time.sleep(0.3)

                elif radios:
                    # -- RADIO BUTTON LOGIC --
                    if value == "":
                        for r in radios:
                            if r.is_selected():
                                parent_label = r.find_element(By.XPATH, "./..")
                                self.driver.execute_script("arguments[0].click();", parent_label)
                                break
                    else:
                        target_input = wrapper.find_element(By.XPATH,
                                                            f".//label[contains(., '{value}')]//input[@type='radio']")
                        if not target_input.is_selected():
                            parent_label = target_input.find_element(By.XPATH, "./..")
                            self.driver.execute_script("arguments[0].click();", parent_label)

            else:
                # -- TEXT OR DATE LOGIC --
                inp = self.wait.until(EC.presence_of_element_located(
                    (By.XPATH, f"{base_xpath}//input[not(@type='radio') and not(@type='checkbox')]")))
                self.driver.execute_script("arguments[0].focus();", inp)
                time.sleep(0.1)

                inp.send_keys(Keys.CONTROL, 'a')
                inp.send_keys(Keys.BACKSPACE)

                if value != "":
                    inp.send_keys(value)

                inp.send_keys(Keys.TAB)

        except Exception as e:
            print(f"  -> Could not fill '{keyword}' with '{value}'. Error: {type(e).__name__}")

    def read_calculated_field(self, keyword: str, is_dropdown: bool = False) -> str:
        """Reads the value from text inputs or React option sets."""
        try:
            base_xpath = f"//div[@data-test='form-field'][contains(., '{keyword}')]"
            wrapper = self.wait.until(EC.presence_of_element_located((By.XPATH, base_xpath)))
            self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", wrapper)

            if is_dropdown:
                vals = wrapper.find_elements(By.XPATH,
                                             ".//*[contains(@class, 'Select-value-label') or contains(@class, 'Select-value') or contains(@class, 'selected-option-label')]")
                return vals[0].text.strip() if (vals and vals[0].text.strip()) else "Blank"
            else:
                inp = wrapper.find_element(By.XPATH, ".//input[@type='text']")
                val = inp.get_attribute("value")
                return val if val else "Blank"
        except Exception as e:
            return "Not found"

    def run_all_tests(self, test_cases: List[TestCase], org_unit_id: str, program_id: str, output_csv: str) -> None:
        self.login()
        results: List[dict] = []

        # 1. Load the form ONCE outside the loop to be super fast
        url = f"{self.base_url}/dhis-web-capture/index.html#/new?orgUnitId={org_unit_id}&programId={program_id}"
        self.driver.get(url)

        print("Waiting 10 seconds for initial DHIS2 metadata load...")
        time.sleep(10)

        for idx, case in enumerate(test_cases, start=1):
            print(f"\nRunning test {idx}/{len(test_cases)}: {case.scenario}")

            try:
                # --- Pre-calculate inputs for Console & CSV ---
                dob_dt = datetime.strptime(case.dob, "%Y-%m-%d")
                today = datetime.today()
                age = today.year - dob_dt.year - ((today.month, today.day) < (dob_dt.month, dob_dt.day))

                height_m = case.height / 100.0
                bmi = round(case.weight / (height_m ** 2), 1)

                smoker_str = "Smoker" if str(case.smoker).lower() == "yes" else "Non-Smoker"
                diab_val = case.diabetes if case.scenario != "Non-lab" else ""
                diab_str = f"Diabetes={diab_val}" if diab_val else "Diabetes=No"
                chol_str = case.chol if case.chol else "Empty"

                print(
                    f"  Inputs: {age}y, {case.sex}, {smoker_str}, {diab_str}, BMI: {bmi}, BP: {case.sbp}/{case.dbp}, Chol: {chol_str}")

                # CASE SENSITIVE KEYWORDS
                self.smart_fill("Birth", case.dob, field_type="date")
                self.smart_fill("Sex", case.sex, field_type="choice")
                self.smart_fill("Smoking", case.smoker, field_type="choice")

                self.smart_fill("hypertension?", "Yes", field_type="choice")
                self.smart_fill("diabetes?", diab_val, field_type="choice")

                self.smart_fill("Height", str(case.height))
                self.smart_fill("Weight", str(case.weight))
                self.smart_fill("Systolic", str(case.sbp))
                self.smart_fill("Diastolic", str(case.dbp))
                self.smart_fill("Cholesterol", str(case.chol))

                self.driver.find_element(By.TAG_NAME, "body").click()
                time.sleep(1.0)  # FAST WAIT: 1s for Program Rules to trigger

                actual_risk = self.read_calculated_field("Risk Score")
                actual_class = self.read_calculated_field("Classification", is_dropdown=True)

                status = "PASS" if (str(actual_risk) == str(case.expected_risk) and str(actual_class).upper() == str(
                    case.expected_class).upper()) else "FAIL"

                print(
                    f"  Expected: {case.expected_risk} {case.expected_class} | Actual: {actual_risk} {actual_class} | Status: {status}")

                results.append({
                    "Scenario": case.scenario,
                    "age": age,
                    "sex": case.sex,
                    "smoking": case.smoker,
                    "diabetes": diab_val if diab_val else "No",
                    "bmi": bmi,
                    "BP": f"{case.sbp}/{case.dbp}",
                    "cholesterol": case.chol,
                    "Expected_Risk": case.expected_risk,
                    "Expected_Class": case.expected_class,
                    "Actual_Risk": actual_risk,
                    "Actual_Class": actual_class,
                    "Status": status,
                })

                time.sleep(0.5)  # Minimal buffer before next test

            except Exception as e:
                print(f"  -> Test failed critically: {e}")

        if results:
            df_out = pd.DataFrame(results)
            cols = ["Scenario", "age", "sex", "smoking", "diabetes", "bmi", "BP", "cholesterol", "Expected_Risk",
                    "Expected_Class", "Actual_Risk", "Actual_Class", "Status"]
            df_out = df_out[cols]
            df_out.to_csv(output_csv, index=False)
            print(f"\nTests complete. Results saved to {output_csv}.")

    def close(self) -> None:
        self.driver.quit()


if __name__ == "__main__":
    BASE_URL, USERNAME, PASSWORD = credentials('SANDBOX_EPIDEMICS')
    ORG_UNIT_ID = "zZjaFOQpCeh"
    PROGRAM_ID = "pMIglSEqPGS"
    OUTPUT_FILE = "DHIS2_Test_Execution_Results.csv"

    test_data: List[TestCase] = [
        TestCase("Non-lab", "1969-01-01", "Female", "No", "", 170, 107, 115, 75, "", "9", "Moderate"),
        TestCase("Non-lab", "1974-01-01", "Female", "No", "", 170, 55, 170, 105, "", "11", "High"),
        TestCase("Non-lab", "1954-01-01", "Male", "No", "", 170, 64, 150, 95, "", "30", "Critical"),
        TestCase("Non-lab", "1964-01-01", "Male", "Yes", "", 170, 55, 130, 85, "", "18", "High"),
        TestCase("Non-lab", "1979-01-01", "Male", "No", "", 170, 92, 170, 105, "", "13", "High"),
        TestCase("Non-lab", "1974-01-01", "Female", "Yes", "", 170, 78, 115, 75, "", "11", "High"),
        TestCase("Non-lab", "1974-01-01", "Female", "No", "", 170, 107, 185, 115, "", "17", "High"),
        TestCase("Non-lab", "1969-01-01", "Female", "No", "", 170, 55, 185, 115, "", "18", "High"),
        TestCase("Non-lab", "1974-01-01", "Male", "Yes", "", 170, 92, 115, 75, "", "13", "High"),
        TestCase("Non-lab", "1969-01-01", "Male", "Yes", "", 170, 64, 130, 85, "", "16", "High"),
        TestCase("Non-lab", "1979-01-01", "Female", "Yes", "", 170, 78, 115, 75, "", "9", "Moderate"),
        TestCase("Non-lab", "1964-01-01", "Male", "No", "", 170, 92, 170, 105, "", "26", "Very High"),
        TestCase("Non-lab", "1979-01-01", "Female", "Yes", "", 170, 64, 185, 115, "", "23", "Very High"),
        TestCase("Non-lab", "1959-01-01", "Male", "No", "", 170, 78, 150, 95, "", "24", "Very High"),
        TestCase("Non-lab", "1964-01-01", "Male", "Yes", "", 170, 92, 115, 75, "", "20", "Very High"),
        TestCase("Non-lab", "1959-01-01", "Male", "No", "", 170, 92, 150, 95, "", "27", "Very High"),
        TestCase("Non-lab", "1954-01-01", "Female", "Yes", "", 170, 78, 115, 75, "", "26", "Very High"),
        TestCase("Non-lab", "1984-01-01", "Female", "No", "", 170, 92, 170, 105, "", "8", "Moderate"),
        TestCase("Non-lab", "1964-01-01", "Female", "Yes", "", 170, 64, 150, 95, "", "24", "Very High"),
        TestCase("Non-lab", "1979-01-01", "Male", "Yes", "", 170, 55, 150, 95, "", "11", "High"),
        TestCase("Lab-no-diabetes", "1984-01-01", "Female", "Yes", "No", 170, 72, 185, 115, "174", "16", "High"),
        TestCase("Lab-no-diabetes", "1959-01-01", "Female", "No", "No", 170, 72, 150, 95, "147", "17", "High"),
        TestCase("Lab-no-diabetes", "1969-01-01", "Female", "Yes", "No", 170, 72, 185, 115, "213", "28", "Very High"),
        TestCase("Lab-no-diabetes", "1959-01-01", "Male", "No", "No", 170, 72, 185, 115, "174", "32", "Critical"),
        TestCase("Lab-no-diabetes", "1954-01-01", "Female", "Yes", "No", 170, 72, 130, 85, "147", "26", "Very High"),
        TestCase("Lab-no-diabetes", "1969-01-01", "Female", "Yes", "No", 170, 72, 170, 105, "251", "25", "Very High"),
        TestCase("Lab-no-diabetes", "1974-01-01", "Male", "No", "No", 170, 72, 115, 75, "251", "7", "Moderate"),
        TestCase("Lab-no-diabetes", "1979-01-01", "Female", "No", "No", 170, 72, 130, 85, "251", "6", "Moderate"),
        TestCase("Lab-no-diabetes", "1959-01-01", "Male", "No", "No", 170, 72, 185, 115, "251", "38", "Critical"),
        TestCase("Lab-no-diabetes", "1969-01-01", "Male", "No", "No", 170, 72, 130, 85, "213", "11", "High"),
        TestCase("Lab-no-diabetes", "1974-01-01", "Male", "No", "No", 170, 72, 150, 95, "278", "14", "High"),
        TestCase("Lab-no-diabetes", "1969-01-01", "Male", "No", "No", 170, 72, 170, 105, "251", "20", "Very High"),
        TestCase("Lab-no-diabetes", "1984-01-01", "Male", "Yes", "No", 170, 72, 170, 105, "147", "11", "High"),
        TestCase("Lab-no-diabetes", "1984-01-01", "Female", "Yes", "No", 170, 72, 150, 95, "147", "9", "Moderate"),
        TestCase("Lab-no-diabetes", "1964-01-01", "Male", "Yes", "No", 170, 72, 185, 115, "278", "48", "Critical"),
        TestCase("Lab-with-diabetes", "1979-01-01", "Male", "Yes", "Yes", 170, 72, 130, 85, "147", "15", "High"),
        TestCase("Lab-with-diabetes", "1974-01-01", "Male", "Yes", "Yes", 170, 72, 115, 75, "251", "21", "Very High"),
        TestCase("Lab-with-diabetes", "1974-01-01", "Female", "No", "Yes", 170, 72, 150, 95, "251", "17", "High"),
        TestCase("Lab-with-diabetes", "1979-01-01", "Female", "No", "Yes", 170, 72, 170, 105, "251", "18", "High"),
        TestCase("Lab-with-diabetes", "1964-01-01", "Male", "Yes", "Yes", 170, 72, 130, 85, "174", "28", "Very High"),
        TestCase("Lab-with-diabetes", "1954-01-01", "Male", "No", "Yes", 170, 72, 130, 85, "251", "38", "Critical"),
        TestCase("Lab-with-diabetes", "1979-01-01", "Male", "No", "Yes", 170, 72, 130, 85, "174", "10", "High"),
        TestCase("Lab-with-diabetes", "1954-01-01", "Female", "No", "Yes", 170, 72, 185, 115, "278", "47", "Critical"),
        TestCase("Lab-with-diabetes", "1954-01-01", "Female", "Yes", "Yes", 170, 72, 115, 75, "147", "31", "Critical"),
        TestCase("Lab-with-diabetes", "1974-01-01", "Male", "Yes", "Yes", 170, 72, 185, 115, "251", "52", "Critical"),
        TestCase("Lab-with-diabetes", "1984-01-01", "Male", "No", "Yes", 170, 72, 115, 75, "278", "10", "High"),
        TestCase("Lab-with-diabetes", "1984-01-01", "Male", "No", "Yes", 170, 72, 150, 95, "147", "9", "Moderate"),
        TestCase("Lab-with-diabetes", "1954-01-01", "Male", "No", "Yes", 170, 72, 185, 115, "174", "51", "Critical"),
        TestCase("Lab-with-diabetes", "1984-01-01", "Male", "Yes", "Yes", 170, 72, 170, 105, "174", "26", "Very High"),
        TestCase("Lab-with-diabetes", "1979-01-01", "Male", "Yes", "Yes", 170, 72, 185, 115, "147", "33", "Critical"),
    ]

    tester = Dhis2RiskTester(BASE_URL, USERNAME, PASSWORD)
    try:
        tester.run_all_tests(test_data, ORG_UNIT_ID, PROGRAM_ID, OUTPUT_FILE)
    finally:
        tester.close()