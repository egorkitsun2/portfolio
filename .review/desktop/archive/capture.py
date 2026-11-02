from pathlib import Path
from PIL import ImageGrab
import json,hashlib,datetime
from src import config
ROOT=Path(__file__).resolve().parents[3]
config.DATA_DIR=Path(__file__).parent/'settings'
config.SETTINGS_FILE=config.DATA_DIR/'settings.json'
from src.gui.app import ArchiveSorterApp, ctk
ctk.set_widget_scaling(0.8)
ctk.set_window_scaling(0.8)
app=ArchiveSorterApp()
images=[]
def capture_sort():
    app.log_box.configure(state="normal")
    app.log_box.delete("1.0","end")
    app.log_box.configure(state="disabled")
    app.update_idletasks()
    image=ImageGrab.grab(window=app.winfo_id())
    target=ROOT/'assets/projects/archive-sort.jpg'
    image.save(target,quality=92)
    images.append({'file':str(target.relative_to(ROOT)),'size':list(image.size),'sha256':hashlib.sha256(target.read_bytes()).hexdigest()})
    app.tabs.set(app.tabs._tab_dict.keys().__iter__().__next__())
    app.after(100,finish)
def finish():
    (ROOT/'.review/archive-capture.json').write_text(json.dumps({'capturedAt':datetime.datetime.now(datetime.timezone.utc).isoformat(),'source':'Unmodified application source in an isolated copy, default empty settings; UI scaling 80%; diagnostic log cleared to omit the local 7-Zip path; no processing started','images':images},indent=2),encoding='utf-8')
    app.destroy()
app.after(1600,capture_sort)
app.after(15000,app.destroy)
app.mainloop()
