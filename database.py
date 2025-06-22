import sqlite3

def init_db():
    conn = sqlite3.connect('db.sqlite')
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS applications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        username TEXT,
        type TEXT NOT NULL,
        subtype TEXT,
        to_name TEXT,
        text TEXT NOT NULL,
        status TEXT DEFAULT 'pending',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        publish_date DATE,
        published_at TIMESTAMP
    )
    """)
    conn.commit()

def add_application(data):
    conn = sqlite3.connect('db.sqlite')
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO applications 
    (user_id, username, type, subtype, to_name, text)
    VALUES (?, ?, ?, ?, ?, ?)
    """, (
        data['user_id'],
        data.get('username'),
        data['type'],
        data.get('subtype'),
        data.get('to_name'),
        data['text']
    ))
    conn.commit()
    return cur.lastrowid

def get_approved_unpublished():
    conn = sqlite3.connect('db.sqlite')
    cur = conn.cursor()
    cur.execute("SELECT * FROM applications WHERE status = 'approved' AND published_at IS NULL")
    return cur.fetchall()

def mark_as_published(app_id):
    conn = sqlite3.connect('db.sqlite')
    cur = conn.cursor()
    cur.execute("UPDATE applications SET published_at = CURRENT_TIMESTAMP WHERE id = ?", (app_id,))
    conn.commit()