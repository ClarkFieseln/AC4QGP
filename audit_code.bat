bandit -r ./ > audit/bandit_report.txt
REM safety needs internet connection:
safety check -r requirements.txt > audit/safety_report.txt
