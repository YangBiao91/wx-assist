"""Read-only existing Outlook COM check; never creates Application or mail items."""
import json

import pythoncom
import win32com.client
from outlook_session import connect_outlook

pythoncom.CoInitialize()
result = {"active_outlook_access":"not_attempted", "outlook_created":False,
          "mail_items_read":False, "configuration_changed":False, "mail_sent":False}
try:
    # Disabled launch: factory fallback is limited to a verified existing,
    # same-session Outlook process. No logon or credential operations.
    app,connection=connect_outlook(win32com.client,allow_launch=False)
    result.update(active_outlook_access="attached_existing", version=str(app.Version),
                  connection=connection,
                  account_count=int(app.Session.Accounts.Count), offline=bool(app.Session.Offline),
                  main_explorer_open=app.ActiveExplorer() is not None,
                  accounts=[{"smtp_address":str(app.Session.Accounts.Item(i).SmtpAddress),
                             "account_type":int(app.Session.Accounts.Item(i).AccountType)}
                            for i in range(1,int(app.Session.Accounts.Count)+1)])
except pythoncom.com_error as exc:
    result.update(active_outlook_access="unavailable", hresult=exc.hresult)
finally:
    print(json.dumps(result), flush=True)
    pythoncom.CoUninitialize()
