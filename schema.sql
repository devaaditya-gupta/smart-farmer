-- Smart Farmer Crop-Advisory & Micro-Loan Risk Tracker  (MySQL 8+)
DROP DATABASE IF EXISTS smart_farmer; CREATE DATABASE smart_farmer; USE smart_farmer;

CREATE TABLE farmer(farmer_id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(80) NOT NULL, phone VARCHAR(15) NOT NULL UNIQUE,
  village VARCHAR(60) NOT NULL, registration_date DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  risk_score DECIMAL(5,1) NOT NULL DEFAULT 50, risk_status ENUM('Low','Medium','High') NOT NULL DEFAULT 'Medium');
CREATE TABLE crop(crop_id INT AUTO_INCREMENT PRIMARY KEY, crop_name VARCHAR(50) NOT NULL UNIQUE, type VARCHAR(30) NOT NULL,
  ideal_rainfall INT NOT NULL, max_rainfall INT NOT NULL, CHECK(max_rainfall>=ideal_rainfall));
CREATE TABLE crop_cycle(cycle_id INT AUTO_INCREMENT PRIMARY KEY, farmer_id INT NOT NULL, crop_id INT NOT NULL,
  area DECIMAL(6,2) NOT NULL CHECK(area>0), sowing_date DATE NOT NULL, harvest_date DATE, yield_qty DECIMAL(10,2),
  status ENUM('Growing','Good','Poor') NOT NULL DEFAULT 'Growing', UNIQUE(cycle_id,farmer_id),
  FOREIGN KEY(farmer_id) REFERENCES farmer(farmer_id), FOREIGN KEY(crop_id) REFERENCES crop(crop_id));
CREATE TABLE fund(fund_id INT PRIMARY KEY, lender VARCHAR(80) NOT NULL, available_balance DECIMAL(12,2) NOT NULL CHECK(available_balance>=0));
CREATE TABLE loan(loan_id INT AUTO_INCREMENT PRIMARY KEY, farmer_id INT NOT NULL, cycle_id INT NULL, fund_id INT NOT NULL DEFAULT 1,
  amount DECIMAL(10,2) NOT NULL CHECK(amount>0), disbursement_date DATE NOT NULL, due_date DATE NOT NULL,
  status ENUM('Active','Repaid','Overdue') NOT NULL DEFAULT 'Active',
  FOREIGN KEY(farmer_id) REFERENCES farmer(farmer_id), FOREIGN KEY(fund_id) REFERENCES fund(fund_id),
  FOREIGN KEY(cycle_id,farmer_id) REFERENCES crop_cycle(cycle_id,farmer_id));   -- a loan can never use another farmer's cycle
CREATE TABLE repayment(repayment_id INT AUTO_INCREMENT PRIMARY KEY, loan_id INT NOT NULL,
  amount_paid DECIMAL(10,2) NOT NULL CHECK(amount_paid>0), payment_date DATE NOT NULL, FOREIGN KEY(loan_id) REFERENCES loan(loan_id));
CREATE TABLE installment(inst_id INT AUTO_INCREMENT PRIMARY KEY, loan_id INT NOT NULL, seq INT NOT NULL, due_date DATE NOT NULL,
  amount DECIMAL(10,2) NOT NULL CHECK(amount>0), paid DECIMAL(10,2) NOT NULL DEFAULT 0,
  status ENUM('Pending','Paid','Overdue') NOT NULL DEFAULT 'Pending', UNIQUE(loan_id,seq), FOREIGN KEY(loan_id) REFERENCES loan(loan_id));
CREATE TABLE risk_log(log_id INT AUTO_INCREMENT PRIMARY KEY, farmer_id INT NOT NULL, score DECIMAL(5,1) NOT NULL,
  status VARCHAR(10) NOT NULL, logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(farmer_id) REFERENCES farmer(farmer_id));

CREATE TABLE officer(officer_id INT AUTO_INCREMENT PRIMARY KEY, username VARCHAR(40) UNIQUE NOT NULL,
  password_hash VARCHAR(255) NOT NULL, full_name VARCHAR(80) NOT NULL);
CREATE TABLE notification(notif_id INT AUTO_INCREMENT PRIMARY KEY, kind VARCHAR(20) NOT NULL, data VARCHAR(500) NOT NULL,
  farmer_id INT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, is_read TINYINT(1) NOT NULL DEFAULT 0);

DELIMITER //
-- Risk score /100 = on-time repayment 50 + overdue history 20 + yield outcomes 20 + loan-to-land 10
CREATE PROCEDURE sp_calc_risk(IN p_fid INT)
BEGIN
  DECLARE v_total, v_repaid, v_over, v_cyc, v_good INT DEFAULT 0;
  DECLARE v_area, v_open DECIMAL(12,2) DEFAULT 0;
  DECLARE v_score DECIMAL(5,1); DECLARE v_status VARCHAR(10);
  SELECT COUNT(*), COALESCE(SUM(status='Repaid'),0), COALESCE(SUM(status='Overdue'),0) INTO v_total,v_repaid,v_over FROM loan WHERE farmer_id=p_fid;
  SELECT COUNT(*), COALESCE(SUM(status='Good'),0) INTO v_cyc,v_good FROM crop_cycle WHERE farmer_id=p_fid AND status<>'Growing';
  SELECT COALESCE(SUM(area),0) INTO v_area FROM crop_cycle WHERE farmer_id=p_fid;
  SELECT COALESCE(SUM(amount),0) INTO v_open FROM loan WHERE farmer_id=p_fid AND status<>'Repaid';
  SET v_score = 50*IF(v_total=0,0.5,v_repaid/v_total) + 20*GREATEST(0,1-v_over/2) + 20*IF(v_cyc=0,0.5,v_good/v_cyc)
              + 10*IF(v_open=0,1,LEAST(1,25000*v_area/v_open));
  SET v_status = IF(v_score>=70,'Low',IF(v_score>=45,'Medium','High'));
  UPDATE farmer SET risk_score=v_score, risk_status=v_status WHERE farmer_id=p_fid;
  INSERT INTO risk_log(farmer_id,score,status) VALUES(p_fid,v_score,v_status);
END//

-- Builds an equal-installment schedule for a loan (last installment carries the rounding remainder)
CREATE PROCEDURE sp_make_schedule(IN p_loan INT, IN p_n INT)
BEGIN
  DECLARE v_amt, v_each DECIMAL(10,2); DECLARE v_d1, v_d2 DATE; DECLARE v_i, v_step INT DEFAULT 1;
  SELECT amount, disbursement_date, due_date INTO v_amt, v_d1, v_d2 FROM loan WHERE loan_id=p_loan;
  SET v_each=ROUND(v_amt/p_n,2), v_step=GREATEST(1,DATEDIFF(v_d2,v_d1) DIV p_n);
  WHILE v_i<=p_n DO
    INSERT INTO installment(loan_id,seq,due_date,amount)
      VALUES(p_loan, v_i, IF(v_i=p_n, v_d2, DATE_ADD(v_d1, INTERVAL v_step*v_i DAY)), IF(v_i=p_n, v_amt-v_each*(p_n-1), v_each));
    SET v_i=v_i+1;
  END WHILE;
END//

-- Applies a payment to the oldest unpaid installments first
CREATE PROCEDURE sp_apply_payment(IN p_loan INT, IN p_amt DECIMAL(10,2))
BEGIN
  DECLARE v_id INT; DECLARE v_open, v_take DECIMAL(10,2); DECLARE v_left DECIMAL(10,2) DEFAULT p_amt;
  WHILE v_left>0 DO
    SET v_id=NULL;
    SELECT inst_id, amount-paid INTO v_id, v_open FROM installment WHERE loan_id=p_loan AND status<>'Paid' ORDER BY seq LIMIT 1;
    IF v_id IS NULL THEN
      SET v_left=0;
    ELSE
      SET v_take=LEAST(v_left,v_open);
      UPDATE installment SET status=IF(paid+v_take>=amount,'Paid',status), paid=paid+v_take WHERE inst_id=v_id;
      SET v_left=v_left-v_take;
    END IF;
  END WHILE;
END//

-- Trigger 1: a repayment is applied to the installments, closes (or reopens) the loan, then refreshes the score
CREATE TRIGGER trg_repayment_ai AFTER INSERT ON repayment FOR EACH ROW
BEGIN
  DECLARE v_paid, v_amt DECIMAL(12,2); DECLARE v_fid INT; DECLARE v_status VARCHAR(10); DECLARE v_due DATE;
  CALL sp_apply_payment(NEW.loan_id, NEW.amount_paid);
  SELECT SUM(amount_paid) INTO v_paid FROM repayment WHERE loan_id=NEW.loan_id;
  SELECT amount, farmer_id, status, due_date INTO v_amt, v_fid, v_status, v_due FROM loan WHERE loan_id=NEW.loan_id;
  IF v_paid>=v_amt THEN
    UPDATE loan SET status='Repaid' WHERE loan_id=NEW.loan_id;
  ELSEIF v_status='Overdue' AND v_due>=CURDATE()
         AND NOT EXISTS(SELECT 1 FROM installment WHERE loan_id=NEW.loan_id AND status='Overdue') THEN
    UPDATE loan SET status='Active' WHERE loan_id=NEW.loan_id;
  END IF;
  CALL sp_calc_risk(v_fid);
END//

-- Trigger 2: a loan turning Overdue refreshes the farmer's risk automatically
CREATE TRIGGER trg_loan_au AFTER UPDATE ON loan FOR EACH ROW
BEGIN
  IF NEW.status='Overdue' AND OLD.status<>'Overdue' THEN CALL sp_calc_risk(NEW.farmer_id); END IF;
END//

CREATE PROCEDURE sp_flag_overdue()
BEGIN
  UPDATE installment SET status='Overdue' WHERE status='Pending' AND due_date<CURDATE();
  UPDATE loan SET status='Overdue' WHERE status='Active'
    AND (due_date<CURDATE() OR loan_id IN (SELECT loan_id FROM installment WHERE status='Overdue'));
END//

-- Transaction: lock the fund, check balance, record the loan and its installment schedule, commit (or roll back)
CREATE PROCEDURE sp_disburse_loan(IN p_fid INT, IN p_cid INT, IN p_amt DECIMAL(10,2), IN p_days INT, IN p_n INT, OUT p_msg VARCHAR(120))
main: BEGIN
  DECLARE v_bal DECIMAL(12,2); DECLARE v_loan INT;
  DECLARE EXIT HANDLER FOR SQLEXCEPTION BEGIN ROLLBACK; SET p_msg='Rejected: unknown farmer, or crop cycle does not belong to this farmer'; END;
  START TRANSACTION;
  SELECT available_balance INTO v_bal FROM fund WHERE fund_id=1 FOR UPDATE;
  IF v_bal<p_amt THEN ROLLBACK; SET p_msg='Rejected: insufficient fund balance'; LEAVE main; END IF;
  UPDATE fund SET available_balance=available_balance-p_amt WHERE fund_id=1;
  INSERT INTO loan(farmer_id,cycle_id,amount,disbursement_date,due_date) VALUES(p_fid,p_cid,p_amt,CURDATE(),DATE_ADD(CURDATE(),INTERVAL p_days DAY));
  SET v_loan=LAST_INSERT_ID();
  CALL sp_make_schedule(v_loan, p_n);
  COMMIT;
  CALL sp_calc_risk(p_fid);
  SET p_msg='OK';
END//
DELIMITER ;

CREATE VIEW v_eligible_farmers AS
  SELECT f.farmer_id, f.name, f.village, f.risk_score, f.risk_status FROM farmer f
  WHERE f.risk_score>=70 AND NOT EXISTS(SELECT 1 FROM loan l WHERE l.farmer_id=f.farmer_id AND l.status='Overdue');

-- Sample data
INSERT INTO fund VALUES(1,'Vellore Farmers Cooperative',420000);
INSERT INTO crop(crop_name,type,ideal_rainfall,max_rainfall) VALUES('Paddy','Cereal',1200,2000),('Wheat','Cereal',650,1000),
  ('Sugarcane','Cash crop',1500,2500),('Groundnut','Oilseed',600,1000),('Cotton','Cash crop',800,1200);
INSERT INTO farmer(name,phone,village) VALUES('Ravi Kumar','9840011001','Katpadi'),('Suresh Babu','9840011002','Gudiyatham'),
  ('Lakshmi Devi','9840011003','Arcot'),('Murugan S','9840011004','Walajapet'),('Anitha R','9840011005','Vellore'),('Karthik P','9840011006','Ambur');
INSERT INTO crop_cycle(farmer_id,crop_id,area,sowing_date,harvest_date,yield_qty,status) VALUES
 (1,1,2.5,DATE_SUB(CURDATE(),INTERVAL 300 DAY),DATE_SUB(CURDATE(),INTERVAL 180 DAY),5200,'Good'),
 (1,2,1.5,DATE_SUB(CURDATE(),INTERVAL 40 DAY),NULL,NULL,'Growing'),
 (2,4,1.5,DATE_SUB(CURDATE(),INTERVAL 280 DAY),DATE_SUB(CURDATE(),INTERVAL 170 DAY),700,'Poor'),
 (3,1,3.0,DATE_SUB(CURDATE(),INTERVAL 290 DAY),DATE_SUB(CURDATE(),INTERVAL 170 DAY),6900,'Good'),
 (4,3,4.0,DATE_SUB(CURDATE(),INTERVAL 400 DAY),DATE_SUB(CURDATE(),INTERVAL 200 DAY),38000,'Good'),
 (4,5,2.0,DATE_SUB(CURDATE(),INTERVAL 150 DAY),DATE_SUB(CURDATE(),INTERVAL 20 DAY),900,'Poor'),
 (5,2,1.2,DATE_SUB(CURDATE(),INTERVAL 30 DAY),NULL,NULL,'Growing'),
 (6,1,2.0,DATE_SUB(CURDATE(),INTERVAL 260 DAY),DATE_SUB(CURDATE(),INTERVAL 150 DAY),2100,'Poor');
INSERT INTO loan(farmer_id,cycle_id,amount,disbursement_date,due_date,status) VALUES
 (1,1,20000,DATE_SUB(CURDATE(),INTERVAL 300 DAY),DATE_SUB(CURDATE(),INTERVAL 150 DAY),'Active'),
 (1,2,15000,DATE_SUB(CURDATE(),INTERVAL 35 DAY),DATE_ADD(CURDATE(),INTERVAL 120 DAY),'Active'),
 (2,3,18000,DATE_SUB(CURDATE(),INTERVAL 270 DAY),DATE_SUB(CURDATE(),INTERVAL 90 DAY),'Overdue'),
 (2,NULL,10000,DATE_SUB(CURDATE(),INTERVAL 200 DAY),DATE_SUB(CURDATE(),INTERVAL 40 DAY),'Overdue'),
 (3,4,25000,DATE_SUB(CURDATE(),INTERVAL 285 DAY),DATE_SUB(CURDATE(),INTERVAL 140 DAY),'Active'),
 (4,5,30000,DATE_SUB(CURDATE(),INTERVAL 390 DAY),DATE_SUB(CURDATE(),INTERVAL 210 DAY),'Active'),
 (4,6,12000,DATE_SUB(CURDATE(),INTERVAL 140 DAY),DATE_ADD(CURDATE(),INTERVAL 60 DAY),'Active'),
 (6,8,22000,DATE_SUB(CURDATE(),INTERVAL 250 DAY),DATE_SUB(CURDATE(),INTERVAL 60 DAY),'Overdue');
CALL sp_make_schedule(1,2); CALL sp_make_schedule(2,3); CALL sp_make_schedule(3,3); CALL sp_make_schedule(4,2);
CALL sp_make_schedule(5,2); CALL sp_make_schedule(6,3); CALL sp_make_schedule(7,3); CALL sp_make_schedule(8,4);
INSERT INTO repayment(loan_id,amount_paid,payment_date) VALUES
 (1,12000,DATE_SUB(CURDATE(),INTERVAL 200 DAY)),(1,8000,DATE_SUB(CURDATE(),INTERVAL 160 DAY)),
 (5,25000,DATE_SUB(CURDATE(),INTERVAL 150 DAY)),(6,30000,DATE_SUB(CURDATE(),INTERVAL 215 DAY)),(7,3000,DATE_SUB(CURDATE(),INTERVAL 30 DAY));
CALL sp_flag_overdue();
CALL sp_calc_risk(1); CALL sp_calc_risk(2); CALL sp_calc_risk(3); CALL sp_calc_risk(4); CALL sp_calc_risk(5); CALL sp_calc_risk(6);
