from __future__ import annotations

import random
import sqlite3
from datetime import date, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "demo_his.sqlite"


def main() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE patient (
            patient_id INTEGER PRIMARY KEY,
            name TEXT,
            gender TEXT,
            id_card TEXT,
            mobile TEXT,
            birth_date TEXT,
            address TEXT
        );

        CREATE TABLE visit (
            visit_id INTEGER PRIMARY KEY,
            patient_id INTEGER,
            org_code TEXT,
            dept_code TEXT,
            visit_type TEXT,
            visit_date TEXT,
            diagnosis_code TEXT,
            total_fee REAL,
            FOREIGN KEY (patient_id) REFERENCES patient(patient_id)
        );

        CREATE TABLE lab_result (
            result_id INTEGER PRIMARY KEY,
            visit_id INTEGER,
            item_code TEXT,
            result_value TEXT,
            result_num REAL,
            report_time TEXT,
            FOREIGN KEY (visit_id) REFERENCES visit(visit_id)
        );
        """
    )
    names = ["张明", "李华", "王芳", "赵敏", "陈强", "刘洋", "周宁", "吴磊"]
    genders = ["男", "女", "未知", ""]
    orgs = ["A001", "A002", "B001"]
    depts = ["CARD", "RESP", "ENDO", "ER"]
    diagnoses = ["I10", "E11", "J18", "R50", "未知", "-"]
    for pid in range(1, 301):
        name = random.choice(names)
        gender = random.choices(genders, weights=[46, 46, 4, 4])[0]
        mobile = f"138{random.randint(10000000, 99999999)}" if random.random() > 0.08 else None
        id_card = f"110101{random.randint(1960, 2020)}{random.randint(1, 12):02d}{random.randint(1, 28):02d}{random.randint(1000, 9999)}"
        birth = date(1960, 1, 1) + timedelta(days=random.randint(0, 22000))
        address = random.choice(["北京市朝阳区", "上海市浦东新区", "未知", None])
        cur.execute(
            "INSERT INTO patient VALUES (?, ?, ?, ?, ?, ?, ?)",
            [pid, name, gender, id_card, mobile, birth.isoformat(), address],
        )
    visit_id = 1
    result_id = 1
    for pid in range(1, 301):
        for _ in range(random.randint(1, 4)):
            visit_date = date(2025, 1, 1) + timedelta(days=random.randint(0, 420))
            cur.execute(
                "INSERT INTO visit VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    visit_id,
                    pid,
                    random.choice(orgs),
                    random.choice(depts),
                    random.choice(["门诊", "住院", "急诊", "未知"]),
                    visit_date.isoformat(),
                    random.choice(diagnoses),
                    round(random.uniform(80, 9000), 2),
                ],
            )
            for item in random.sample(["HB", "WBC", "PLT", "ALT", "CRP"], random.randint(1, 5)):
                value = random.choice(["正常", "偏高", "偏低", "未见异常", "-", None])
                num = round(random.uniform(1, 200), 2) if value not in {"-", None} else None
                cur.execute(
                    "INSERT INTO lab_result VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        result_id,
                        visit_id,
                        item,
                        value,
                        num,
                        (visit_date + timedelta(days=random.randint(0, 3))).isoformat(),
                    ],
                )
                result_id += 1
            visit_id += 1
    conn.commit()
    conn.close()
    print(DB_PATH)


if __name__ == "__main__":
    main()
