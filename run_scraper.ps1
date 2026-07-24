Set-Location "D:\coding projects\saiprice"
$timestamp = Get-Date -Format "yyyy-MM-dd_HHmm"
$report = "scraper_run_reports\${timestamp}_crawl.txt"
& ".\venv\Scripts\python.exe" manage.py scrape_listings --source alonhadat *>> $report
& ".\venv\Scripts\python.exe" manage.py score_listings *>> $report
