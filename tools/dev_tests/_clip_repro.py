import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QImage, QColor
app = QApplication([])
cb = QApplication.clipboard()
img = QImage(10, 10, QImage.Format_ARGB32); img.fill(QColor("red"))
cb.setImage(img)
cb.setText("x")
print("clipboard done", flush=True)
