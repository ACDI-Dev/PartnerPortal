import io
import json
import traceback
from datetime import datetime, timezone
from flask import Blueprint, render_template, request, jsonify, send_file
from auth.fusionauth import login_required
from integrations.gcs_client import load_employee_data, get_gcs_client, BUCKET_NAME, BLOB_NAME

employees_bp = Blueprint('employees', __name__)

@employees_bp.route('/employees', methods=['GET'])
@login_required
def manage_employees(user):
    employees = load_employee_data()
    return render_template('employees.html', employees=employees)

@employees_bp.route('/employees/save_cloud', methods=['POST'])
def save_to_cloud():
    try:
        updated_data = request.get_json(force=True)
        current_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        for emp in updated_data:
            emp["lastUpdateDate"] = current_time
            first, last = emp.get('First Name', '').strip(), emp.get('Last Name', '').strip()
            emp["Full Name"] = f"{first} {last}".strip()

        json_output = json.dumps(updated_data, indent=4)
        client = get_gcs_client()
        bucket = client.bucket(BUCKET_NAME)
        blob = bucket.blob(BLOB_NAME)
        
        if blob.exists():
            timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            bucket.copy_blob(blob, bucket, f"ID/backup/employees_{timestamp_str}.json")
        
        blob.upload_from_string(json_output, content_type='application/json')
        blob.cache_control = 'no-cache, max-age=0, must-revalidate'
        blob.patch()

        return jsonify({"status": "success", "message": "Saved successfully!"}), 200
    except Exception as e:
        traceback.print_exc()
        return jsonify({"status": "error", "message": str(e)}), 400

@employees_bp.route('/employees/download', methods=['POST'])
def download_updated_json():
    try:
        updated_data = request.get_json(force=True) 
        current_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        for emp in updated_data:
            emp["lastUpdateDate"] = current_time
            first, last = emp.get('First Name', '').strip(), emp.get('Last Name', '').strip()
            emp["Full Name"] = f"{first} {last}".strip()

        buffer = io.BytesIO(json.dumps(updated_data, indent=4).encode('utf-8'))
        return send_file(buffer, as_attachment=True, download_name='employees.json', mimetype='application/json')
    except Exception as e:
        traceback.print_exc() 
        return jsonify({"status": "error", "message": str(e)}), 400

@employees_bp.route('/employees/list_bucket_images', methods=['GET'])
def list_bucket_images():
    try:
        prefix = request.args.get('prefix', 'ID/')
        client = get_gcs_client()
        bucket = client.bucket(BUCKET_NAME)
        blobs = bucket.list_blobs(prefix=prefix)
        valid_extensions = ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg')
        
        file_list = [
            {'path': b.name, 'filename': b.name.split('/')[-1], 'url': f"https://storage.googleapis.com/{BUCKET_NAME}/{b.name}"}
            for b in blobs if b.name.lower().endswith(valid_extensions)
        ]
        return jsonify({'status': 'success', 'files': file_list}), 200
    except Exception as e:
        traceback.print_exc()
        return jsonify({'status': 'error', 'message': str(e)}), 400

@employees_bp.route('/upload-image', methods=['POST'])
def upload_image():
    try:
        file = request.files.get('profile_image')
        first_name, last_name, tag_name = request.form.get('first_name'), request.form.get('last_name'), request.form.get('tag_name')

        if not file:
            return jsonify({"error": "No file part"}), 400

        extension = file.filename.rsplit('.', 1)[1].lower() if '.' in file.filename else 'png'
        blob_path = f"ID/{first_name} {last_name}".strip() + f"/{first_name}_{last_name}_{tag_name}.{extension}"

        client = get_gcs_client()
        bucket = client.bucket(BUCKET_NAME)
        blob = bucket.blob(blob_path)
        blob.upload_from_file(file, content_type=file.content_type)
        blob.cache_control = 'no-cache, max-age=0, must-revalidate'
        blob.patch()

        return jsonify({"status": "success", "public_path": f"https://storage.googleapis.com/{BUCKET_NAME}/{blob_path}"}), 200
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@employees_bp.route('/directory')
@login_required
def employee_directory(user):
    raw_employees = load_employee_data()
    emp_by_email = {emp.get('Email Address', '').strip().lower(): emp for emp in raw_employees if emp.get('Email Address')}
    
    leadership_team_members, raw_divisions = [], {}
    
    for emp in raw_employees:
        if emp.get('status') != 'active':
            continue
        
        team_name = emp.get('Team', '').strip() or 'Core Team'
        if team_name == 'Leadership Team':
            leadership_team_members.append(emp)
            
        division_name = emp.get('Division', '').strip() or 'General Operations'
        dept_name = emp.get('Department', '').strip() or 'General Department'
        
        raw_divisions.setdefault(division_name, {}).setdefault(dept_name, {}).setdefault(team_name, {})
            
        manager_email = emp.get('Manager Email', '').strip().lower()
        if manager_email and manager_email in emp_by_email:
            mgr = emp_by_email[manager_email]
            manager_name = f"{mgr.get('First Name', '')} {mgr.get('Last Name', '')}".strip() + f" ({mgr.get('Employee Title', 'Manager')})"
        elif manager_email:
            manager_name = f"Manager ({manager_email})"
        else:
            manager_name = "Executive / Direct Leadership"
            
        raw_divisions[division_name][dept_name][team_name].setdefault(manager_name, []).append(emp)

    divisions = {}
    for div_name, departments in raw_divisions.items():
        sorted_depts = {}
        sorted_dept_keys = sorted(departments.keys(), key=lambda d: (0 if d == 'Leadership Team' else 1, d))
        for dept_name in sorted_dept_keys:
            teams = departments[dept_name]
            sorted_team_keys = sorted(teams.keys(), key=lambda t: (0 if 'manager' in t.lower() else 1, t))
            sorted_depts[dept_name] = {t_name: teams[t_name] for t_name in sorted_team_keys}
        divisions[div_name] = sorted_depts

    return render_template('directory.html', user=user, divisions=divisions, leadership_team=leadership_team_members)