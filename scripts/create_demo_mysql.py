from __future__ import annotations

import random
import sys
from datetime import date, timedelta

import pymysql

from app.datasource import parse_mysql_uri


DDL = """
DROP TABLE IF EXISTS lab_result;
DROP TABLE IF EXISTS visit;
DROP TABLE IF EXISTS patient;

CREATE TABLE patient (
    patient_id INT PRIMARY KEY,
    name VARCHAR(50),
    gender VARCHAR(20),
    id_card VARCHAR(32),
    mobile VARCHAR(32),
    birth_date DATE,
    address VARCHAR(200)
) COMMENT='患者主索引';

CREATE TABLE visit (
    visit_id INT PRIMARY KEY,
    patient_id INT,
    org_code VARCHAR(20),
    dept_code VARCHAR(20),
    visit_type VARCHAR(20),
    visit_date DATE,
    diagnosis_code VARCHAR(20),
    total_fee DECIMAL(12, 2),
    CONSTRAINT fk_visit_patient FOREIGN KEY (patient_id) REFERENCES patient(patient_id)
) COMMENT='就诊记录';

CREATE TABLE lab_result (
    result_id INT PRIMARY KEY,
    visit_id INT,
    item_code VARCHAR(20),
    result_value VARCHAR(50),
    result_num DECIMAL(12, 2),
    report_time DATETIME,
    CONSTRAINT fk_lab_result_visit FOREIGN KEY (visit_id) REFERENCES visit(visit_id)
) COMMENT='检验结果';
"""


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python scripts/create_demo_mysql.py mysql://user:password@127.0.0.1:3306/datapulse_demo")
    cfg = parse_mysql_uri(sys.argv[1])
    conn = pymysql.connect(
        host=cfg["host"],
        port=cfg["port"],
        user=cfg["user"],
        password=cfg["password"],
        database=cfg["database"],
        charset="utf8mb4",
        autocommit=False,
    )
    cur = conn.cursor()
    for statement in DDL.strip().split(";"):
        if statement.strip():
            cur.execute(statement)
    names = ["张明", "李华", "王芳", "赵敏", "陈强", "刘洋", "周宁", "吴磊"]
    genders = ["男", "女", "未知", ""]
    orgs = ["A001", "A002", "B001"]
    depts = ["CARD", "RESP", "ENDO", "ER"]
    diagnoses = ["I10", "E11", "J18", "R50", "未知", "-"]
    for pid in range(1, 301):
        birth = date(1960, 1, 1) + timedelta(days=random.randint(0, 22000))
        cur.execute(
            "INSERT INTO patient VALUES (%s, %s, %s, %s, %s, %s, %s)",
            [
                pid,
                random.choice(names),
                random.choices(genders, weights=[46, 46, 4, 4])[0],
                f"110101{random.randint(1960, 2020)}{random.randint(1, 12):02d}{random.randint(1, 28):02d}{random.randint(1000, 9999)}",
                f"138{random.randint(10000000, 99999999)}" if random.random() > 0.08 else None,
                birth,
                random.choice(["北京市朝阳区", "上海市浦东新区", "未知", None]),
            ],
        )
    visit_id = 1
    result_id = 1
    for pid in range(1, 301):
        for _ in range(random.randint(1, 4)):
            visit_date = date(2025, 1, 1) + timedelta(days=random.randint(0, 420))
            cur.execute(
                "INSERT INTO visit VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                [
                    visit_id,
                    pid,
                    random.choice(orgs),
                    random.choice(depts),
                    random.choice(["门诊", "住院", "急诊", "未知"]),
                    visit_date,
                    random.choice(diagnoses),
                    round(random.uniform(80, 9000), 2),
                ],
            )
            for item in random.sample(["HB", "WBC", "PLT", "ALT", "CRP"], random.randint(1, 5)):
                value = random.choice(["正常", "偏高", "偏低", "未见异常", "-", None])
                cur.execute(
                    "INSERT INTO lab_result VALUES (%s, %s, %s, %s, %s, %s)",
                    [
                        result_id,
                        visit_id,
                        item,
                        value,
                        round(random.uniform(1, 200), 2) if value not in {"-", None} else None,
                        visit_date + timedelta(days=random.randint(0, 3)),
                    ],
                )
                result_id += 1
            visit_id += 1
    conn.commit()
    conn.close()
    print(f"Seeded MySQL demo database: {cfg['database']}")


if __name__ == "__main__":
    main()
