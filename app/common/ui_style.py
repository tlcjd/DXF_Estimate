TABLE_SELECTION_STYLE = """
QTableView, QTreeView {
    selection-background-color: #17365D;
    selection-color: #FFFFFF;
}
QTableView::item:selected, QTreeView::item:selected {
    background-color: #17365D;
    color: #FFFFFF;
}
QTableView::item:hover:!selected, QTreeView::item:hover:!selected {
    background-color: #FFF2CC;
    color: #111111;
}
QTableView::item:focus, QTreeView::item:focus {
    border: 2px solid #F28C00;
}
QHeaderView::section:hover {
    background-color: #FFE699;
    color: #111111;
}
"""


def apply_global_table_style(app):
    """표·트리의 선택행, 현재 셀, 마우스 위치를 서로 다른 색으로 구분한다."""
    existing = str(app.styleSheet() or "")
    app.setStyleSheet(existing + "\n" + TABLE_SELECTION_STYLE)
