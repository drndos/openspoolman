import requests
import json
from logger import log

# Code from https://github.com/greghesp/ha-bambulab/issues/673#issuecomment-2455955428

# Prompt the user for their Bambu Lab username and password
bambuUsername = input("Enter your Bambu Lab username: ")
bambuPassword = input("Enter your Bambu Lab password: ")

# Define the User-Agent and other common headers
user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/103.0.0.0 Safari/537.36"
headers = {
  "Content-Type": "application/json",
  "User-Agent": user_agent,
  "Accept": "application/json, text/plain, */*",
  "Accept-Language": "en-US,en;q=0.9",
  "Origin": "https://bambulab.com",
  "Referer": "https://bambulab.com/"
}

# Perform the login request with custom headers
auth_payload = {
  "account": bambuUsername,
  "password": bambuPassword
}
auth_response = requests.post("https://api.bambulab.com/v1/user-service/user/login", headers=headers, json=auth_payload)

# Print the auth response for debugging
log("Step 1. Auth login response:", auth_response.text)

# Check if authentication was successful
auth_json = auth_response.json()
if not auth_json.get("success"):
  log("Authentication failed, attempting code verification")

  # Send the verification code request
  send_code_payload = {
    "email": bambuUsername,
    "type": "codeLogin"
  }

  if auth_json.get("loginType") == "verifyCode":
    send_code_response = requests.post("https://api.bambulab.com/v1/user-service/user/sendemail/code", headers=headers,
                                       json=send_code_payload)
    if send_code_response.status_code == 200:
      log("Verification code sent successfully. Check your email.")
    else:
      log("Failed to send verification code.")
      log("Response:", send_code_response.text)
      exit(1)

    verify_code = input("Enter your access code: ")
    verify_payload = {
      "account": bambuUsername,
      "code": verify_code
    }
    api_response = requests.post("https://api.bambulab.com/v1/user-service/user/login", headers=headers,
                                 json=verify_payload)
    log("Step 2 - API verify code response:", api_response.text)

    api_token = api_response.json()
    if api_token:
      token = api_token.get("accessToken")

    if not token:
      log("Failed to extract token")
      exit(1)

  elif auth_json.get("loginType") == "tfa":
    tfa_auth = auth_json.get("tfaKey")
    tfa_code = input("Enter your MFA access code: ")
    verify_payload = {
      "tfaKey": tfa_auth,
      "tfaCode": tfa_code
    }
    log("payload: ", verify_payload)
    api2_response = requests.post("https://bambulab.com/api-sign-in/tfa", headers=headers, json=verify_payload)
    log("Step 2 - API MFA response:", api2_response.text)

    cookies = api2_response.cookies.get_dict()
    token_from_tfa = cookies.get("token")
    log("tokenFromTFA:", token_from_tfa)

    token = token_from_tfa

    if not token:
      log("Failed to extract token")
      exit(1)

else:
  # If authentication was successful in the first attempt, get the token from the JSON response
  token = auth_json.get("accessToken")

if not token:
  log("Unable to authenticate or verify. Exiting...")
  exit(1)

# Perform the API request to fetch the image with custom headers
headers["Authorization"] = f"Bearer {token}"
api_response = requests.get("https://api.bambulab.com/v1/iot-service/api/user/bind", headers=headers)

# Print the API response for debugging
log("API to fetch info:", api_response.text)

# Check if the API request was successful
if api_response.status_code != 200:
  log("API request failed")
  exit(1)

# Extract and display the relevant information
api_json = api_response.json()

log(json.dumps(api_json, indent=2))
