##############################################################
# Python script to attempt automatic unpacking/decrypting of #
# malware samples using WinAppDbg.                           #
#                                                            #
# unpack.py v2014.06.30                                      #
# http://malwaremusings.com/scripts/unpack.py                #
##############################################################

import sys
import traceback
import winappdbg
import time
import struct


# Log file which we log info to
logfile = None

class MyEventHandler(winappdbg.EventHandler):

###
# A. Declaring variables
###

	# A.1 used to keep track of allocated executable memory
	allocedmem = {}

	# A.2 used to indicate that we've found the entry point
	entrypt = 0x00000000

	#
	# variables used to find and disassemble unpacking loop
	#

	# A.3 used to indicate that we're single stepping
	tracing = {}

	# A.4 remember the last two eip values
	lasteip = [0x00000000,0x00000000]

	# A.5 lowest eip address we see
	lowesteip = 0xffffffff

	# A.6 highest eip address we see
	highesteip = 0x00000000

	# A.7 list of addresses which we've disassembled
	disasmd = []

	# A.8 keeps track of addresses and instructions
	#     that write to the allocated memory block(s)
	writeaddrs = {}


	#
	# variables used to find export symbols
	#

	ntbrkpthit = False
	exportednames = {0x00000000:"invalid"}
	exportdirrdaddrs = {}
	#exportdirs = {}
	#currsym = 0
	#currsymlen = 0
	#currsymnull = 0

	#readnullbyte = True

	membps = {}

	createdprocesses = {}

	eventlog = []


###
# B. Class methods (functions)
###

	### B.1
	# get_funcargs(event)
	#     query winappdbg to get the function arguments
	#
	#     return a tuple consisting of the return address
	#     and a sub-tuple of function arguments
	###

	def get_funcargs(self,event):
		h = event.hook
		t = event.get_thread()
		tid = event.get_tid()

		return (t.get_pc(),h.get_params(tid))


	def get_exportdir_names(self,module):
		name = module.get_name()
		baseaddr = module.get_base()
		p = module.get_process()

		lfa_new = baseaddr + p.read_uint(baseaddr + 0x3c)
		#log("[D]    lfa_new: 0x%x" % lfa_new)
		export_diraddr = baseaddr + p.read_uint(lfa_new + 0x78)
		export_dirsize = p.read_uint(lfa_new + 0x78 + 0x04)
		export_numofnames = p.read_uint(export_diraddr + 0x18)
		export_addressofnames = baseaddr + p.read_uint(export_diraddr + 0x20)

		#log("[D]    BaseAddress: 0x%x" % baseaddr)
		#log("[D]    NumberOfNames: %d" % export_numofnames)
		#log("[D]    AddressOfNames: 0x%x" % export_addressofnames)

		for i in range(0,export_numofnames):
			export_nameaddr = baseaddr + p.read_uint(export_addressofnames + (i * 4))
			export_name = p.peek_string(export_nameaddr)
			self.exportednames[export_nameaddr] = (name,export_name)
			#log("[-]     0x%x: %s.%s" % (export_nameaddr,name,export_name))

		#
		# get addresses to set guard pages
		#
		firstname = baseaddr + p.read_uint(export_addressofnames)
		endofnames = export_diraddr + export_dirsize
		nameslen = endofnames - firstname + 1

		return (firstname,nameslen)


	#
	# pid, start, and end are used to reference pages saved in exportdirs
	# addr is addr of the string. we need to find the end ourselves.
	def read_export_string(self,pid,start,end,addr):
		pagesize = winappdbg.System.pageSize
		if ((pid,start,end) in self.exportdirs):
			pg1 = self.exportdirs[(pid,start,end)]

			if ((pid,end,end + pagesize) in self.exportdirs):
				pg2 = self.exportdirs[(pid,end,end + pagesize)]
			else:
				pg2 = ""
			pgs = pg1 + pg2

			offset = addr - start
			nullbyte = pgs.find("\0",offset)
			symname = pgs[offset:nullbyte]
		else:
			log("[E] read_export_string(): address range not found in exportdirs!")

		return symname


	#
	# For what we are about to read
	# May the Lord make sure 
	# There're no breakpoints
	# Amen
	#
	def guarded_read(self,d,t,addr,size):
		reenablebps = []

		data = ""
		if (size > 0):
			p = t.get_process()

			mem_bps = d.get_all_page_breakpoints()
			for (pid,pgbp) in mem_bps:
				pgbpspan = pgbp.get_span()
				if (pid == p.get_pid()) and (pgbp.is_here(addr) or pgbp.is_here(addr + size - 1)):
					log("[D]   Memory read in guarded memory. Disabling breakpoint: %s" % pgbp)
					pgbp.disable(p,t)
					reenablebps.append(pgbp)

			data = p.read(addr,size)

			if (len(reenablebps) > 0):
				for pgbp in reenablebps:
					log("[D]   Re-enabling breakpoint: %s" % pgbp)
					pgbp.enable(p,t)

		return data


###
# C. API Hooks
###

	### C.1
	# apiHooks: winappdbg defined hash of API calls to hook
	#
	#     Each entry is indexed by library name and is an array of 
	#     tuples consisting of API call name and number of args
	###

	apiHooks = {
		"kernel32.dll":[
			("VirtualAllocEx",5),
			("IsDebuggerPresent",0),
			("CreateProcessA",10),
			("CreateProcessW",10),
			("WriteProcessMemory",5)
		],
		"advapi32.dll":[
			("CryptDecrypt",6)
		],
		"wininet.dll":[
			("InternetOpenA",5),
			("InternetOpenW",5)
		],
		"ntdll.dll":[
			("RtlDecompressBuffer",6)
		]
	}


	###
	# API hook callback functions
	#
	#     These are defined by winappdbg and consist of functions
	#     named pre_<apifuncname> and post_<apifuncname> which are
	#     called on entry to, and on exit from, the given API 
	#     function (<apifuncname>), respectively.
	###

	# C.2
	# VirtualAllocEx() hook(s)
	#

	def post_VirtualAllocEx(self,event,retval):
		try:
			# C.2.1 Get the return address and arguments

			(ra,(hProcess,lpAddress,dwSize,flAllocationType,flProtect)) = self.get_funcargs(event)

			# Get an instance to the debugger which triggered the event
			# and also the process id and thread id of the process to which 
			# the event pertains

			d = event.debug
			pid = event.get_pid()
			tid = event.get_tid()

			# Log the fact that we've seen a VirtualAllocEx() call

			log("[*] <%d:%d> 0x%x: VirtualAllocEx(0x%x,0x%x,0x%x (%d),0x%x,0x%03x) = 0x%x" % (pid,tid,ra,hProcess,lpAddress,dwSize,dwSize,flAllocationType,flProtect,retval))

			# C.2.2 All the memory protection bits which include EXECUTE
			# permission use bits 4 - 7, which is nicely matched 
			# by masking (ANDing) it with 0xf0 and checking for a 
			# non-zero result

			if (flProtect & 0x0f0):
				log("[*]     Request for EXECUTEable memory")

				# We can only set page guards on our own process
				# otherwise page guard exception will occur in 
				# system code when this process attempts to write 
				# to the allocated memory.
				# This causes ZwWriteVirtualMemory() to fail

				# We can, however, set a page guard on it when 
				# this process creates the remote thread, as it 
				# will have presumably stopped writing to the 
				# other process' memory at that point.

				# C.2.2.1 Check that this VirtualAllocEx() call is for
				# the current process (hProcess == -1), and if
				# so, ask the winappdbg debugger instance to 
				# create a page guard on the memory region.
				# Also add information about the allocated region
				# to our allocedmem hash, indexed by pid and 
				# base address.

				if (hProcess == 0xffffffff):
					d.watch_buffer(pid,retval,dwSize - 1,self.guard_page_exemem)
					self.allocedmem[(pid,retval)] = dwSize

			self.eventlog.append({
				"time": time.time(),
				"name": "VirtualAllocEx",
				"type": "Win32 API",
				"pid": pid,
				"tid": tid,
				"addr": ra,
				"args": {
					"hProcess": hProcess,
					"lpAddress": lpAddress,
					"dwSize": dwSize,
					"flAllocationType": flAllocationType,
					"flProtect": flProtect
				},
				"ret": retval
			})
		except:
			traceback.print_exc()
			raise


	def post_IsDebuggerPresent(self,event,retval):
		(ra) = self.get_funcargs(event)

		log("[*] IsDebuggerPresent(): 0x%x" % retval)

		t = event.get_thread()
		t.set_register("Eax",0x0)


	# C.3
	# CryptDecrypt() hook(s)
	#

	def pre_CryptDecrypt(self,event,*args):
		(ra,(hKey,hHash,Final,dwFlags,pbData,pdwDataLen)) = self.get_funcargs(event)

		p = event.get_process()
		buffsize = p.read_uint(pdwDataLen)

		#
		# save a copy of the encrypted data
		#
		filename = "%s.memblk0x%x.enc" % (sys.argv[1],pbData)
		log("[-]    Dumping %d bytes of encrypted memory at 0x%x to %s" % (buffsize,pbData,filename))
		databuff = open(filename,"wb")
		databuff.write(p.read(pbData,buffsize));
		databuff.close()


	def post_CryptDecrypt(self,event,retval):
		(ra,(hKey,hHash,Final,dwFlags,pbData,pdwDataLen)) = self.get_funcargs(event)

		p = event.get_process()
		buffsize = p.read_uint(pdwDataLen)

		#
		# save a copy of the decrypted data
		#
		filename_enc = "%s.memblk0x%x.enc" % (sys.argv[1],pbData)
		filename = "%s.memblk0x%x.dec" % (sys.argv[1],pbData)
		log("[-]    Dumping %d bytes of decrypted memory at 0x%x to %s" % (buffsize,pbData,filename))
		databuff = open(filename,"wb")
		databuff.write(p.read(pbData,buffsize))
		databuff.close()

		pid = event.get_pid()
		tid = event.get_tid()
		self.eventlog.append({
			"time": time.time(),
			"name": "CryptDecrypt",
			"type": "Win32 API",
			"pid": pid,
			"tid": tid,
			"addr": ra,
			"args": {
				"hKey": hKey,
				"hHash": hHash,
				"Final": Final,
				"dwFlags": dwFlags,
				"pbData": pdwDataLen
			},
			"ret": retval,
			"info": {
				"filename_enc": filename_enc,
				"filename_dec": filename
			}
		})


	# C.4
	# InternetOpen*() hook(s)
	#

	def post_InternetOpen(self,event,retval,fUnicode):
		(ra,(lpszAgent,dwAccessType,lpszProxyName,lpszProxyBypass,dwFlags)) = self.get_funcargs(event)

		p = event.get_process()
		szAgent = p.peek_string(lpszAgent,fUnicode) + "\0"
		szProxyName = p.peek_string(lpszProxyName,fUnicode) + "\0"
		szProxyBypass = p.peek_string(lpszProxyBypass,fUnicode) + "\0"

		log("[*] <%d:%d> 0x%x: InternetOpen(\"%s\",0x%x,\"%s\",\"%s\",0x%x) = 0x%x" % (pid,tid,ra,szAgent,dwAccessType,szProxyName,szProxyBypass,dwFlags,retval))


	def post_InternetOpenA(self,event,retval):
		self.post_InternetOpen(event,retval,False)


	def post_InternetOpenW(self,event,retval):
		self.post_InternetOpen(event,retval,True)


	def post_CreateProcess(self,event,retval,fUnicode):
		try:
			(ra,(lpApplicationName,lpCommandLine,lpProcessAttributes,lpThreadAttributes,bInheritHandles,dwCreationFlags,lpEnvironment,lpCurrentDirectory,lpStartupInfo,lpProcessInformation)) = self.get_funcargs(event)

			p = event.get_process()
			t = event.get_thread()

			pid = event.get_pid()
			tid = event.get_tid()

			szApplicationName = p.peek_string(lpApplicationName,fUnicode)
			szCommandLine = p.peek_string(lpCommandLine,fUnicode)

			log("[D]   lpProcessInformation = 0x%x" % lpProcessInformation)
			if (lpProcessInformation):
				d = event.debug
				ProcessInformation = self.guarded_read(d,t,lpProcessInformation,16)

				hProcess = struct.unpack("<L",ProcessInformation[0:4])[0]
				hThread  = struct.unpack("<L",ProcessInformation[4:8])[0]
				dwProcessId = struct.unpack("<L",ProcessInformation[8:12])[0]
				dwThreadId = struct.unpack("<L",ProcessInformation[12:16])[0]
			else:
				log("[E]   lpProcessInformation is null")

			log("[*] <%d:%d> 0x%x: CreateProcess(\"%s\",\"%s\",0x%x): %d (0x%x, 0x%x, <%d:%d>)" % (pid,tid,ra,szApplicationName,szCommandLine,dwCreationFlags,retval,hProcess,hThread,dwProcessId,dwThreadId))

			if (dwCreationFlags & 0x4):
				#
				# CREATE_SUSPENDED
				#
				d = event.debug
				stat = d.break_at(pid,"ResumeThread",self.bp_createprocessresume)
				log("[-]   CREATE_SUSPENDED. Setting breakpoint at ResumeThread() (%d)" % stat)

			self.createdprocesses[hProcess] = {
				"time": time.time(),
				"ppid": pid,
				"ptid": tid,
				"paddr": ra,
				"ApplicationName":szApplicationName,
				"CommandLine": szCommandLine,
				"CreationFlags": dwCreationFlags,
				"hProcess": hProcess,
				"hThread": hThread,
				"ProcessId": dwProcessId,
				"ThreadId": dwThreadId
			}

			self.eventlog.append({
				"time": time.time(),
				"name": "CreateProcess",
				"type": "Win32 API",
				"pid": pid,
				"tid": tid,
				"addr": ra,
				"args": {
					"ApplicationName":szApplicationName,
					"CommandLine": szCommandLine,
					"CreationFlags": dwCreationFlags,
					"hProcess": hProcess,
					"hThread": hThread,
					"ProcessId": dwProcessId,
					"ThreadId": dwThreadId
				},
				"ret": retval
			})

			#d = event.debug
			#d.attach(dwProcessId)
		except:
			traceback.print_exc()
			raise


	def post_CreateProcessA(self,event,retval):
		self.post_CreateProcess(event,retval,False)

	def post_CreateProcessW(self,event,retval):
		self.post_CreateProcess(event,retval,True)


	def post_WriteProcessMemory(self,event,retval):
		try:
			(ra,(hProcess,lpBaseAddress,lpBuffer,nSize,lpNumberOfBytesWritten)) = self.get_funcargs(event)

			log("[*] WriteProcessMemory(0x%x,0x%x,0x%x,0x%x,0x%x): %d" % (hProcess,lpBaseAddress,lpBuffer,nSize,lpNumberOfBytesWritten,retval))

			d = event.debug
			t = event.get_thread()
			if (lpNumberOfBytesWritten):
				NumberOfBytesWritten = struct.unpack("<L",self.guarded_read(d,t,lpNumberOfBytesWritten,4))[0]
			else:
				NumberOfBytesWritten = None

			if (hProcess in self.createdprocesses):
				ProcessId = self.createdprocesses[hProcess]["ProcessId"]
				ApplicationName = self.createdprocesses[hProcess]["ApplicationName"]
				CommandLine = self.createdprocesses[hProcess]["CommandLine"]
			else:
				log("[W]   hProcess not in createdprocesses[]")
				ProcessId = None
				ApplicationName = None
				CommandLine = None

			d = event.debug
			t = event.get_thread()

			pid = event.get_pid()
			tid = event.get_tid()
			filename = "%s.memblk0x%x-%d.wpm" % (sys.argv[1],lpBaseAddress,ProcessId)
			log("[-]    Dumping %d bytes of memory at %d:0x%x written to %d:0x%x to %s" % (nSize,pid,lpBuffer,ProcessId,lpBaseAddress,filename))
			databuff = open(filename,"wb")
			databuff.write(self.guarded_read(d,t,lpBuffer,nSize))
			databuff.close()

			self.eventlog.append({
				"time": time.time(),
				"name": "WriteProcessMemory",
				"type": "Win32 API",
				"pid": pid,
				"tid": tid,
				"addr": ra,
				"args": {
					"hProcess": hProcess,
					"lpBaseAddress": lpBaseAddress,
					"lpBuffer": lpBuffer,
					"nSize": nSize,
					"lpNumberOfBytesWritten": lpNumberOfBytesWritten,
					"NumberOfBytesWritten": NumberOfBytesWritten
				},
				"ret": retval,
				"info": {
					"filename": filename,
					"targetprocesspid": ProcessId,
					"targetprocessname": ApplicationName,
					"targetprocesscmdline": CommandLine
				}
			})
		except:
			traceback.print_exc()
			raise


###
# D. winappdbg debug event handlers
###

	### D.1
	# create_process
	#
	#     winappdbg defined callback function to handle process creation events
	###

	def create_process(self,event):
		try:
			proc = event.get_process()
		
			log("[*] Create process event for pid %d (%s)" % (proc.get_pid(),proc.get_image_name()))
		except:
			traceback.print_exc()
			raise


	### D.2
	# exit_process
	#
	#     winappdbg defined callback function to handle process exit events
	###

	def exit_process(self,event):
		log("[*] Exit process event for pid %d (%s): %d" % (event.get_pid(),event.get_filename(),event.get_exit_code()))


	### D.3
	# create_thread
	#
	#     winappdbg defined callback function to handle thread creation events
	###

	def create_thread(self,event):
		log("[*] Create thread event")


	### D.x
	# membp_exportdir
	#
	###

	# Almost working. Needs debug output removing. Possibly needs tidying
	# Fails to log last symbol searched for, as we don't get another memory access and hence
	# no guard page to log the last symbol
	# Consider checking if subsequent calls increment f_addr all the way to null byte (hashing), or 
	# jump up the symbols (strcmp() behaviour)
	# If jumping, then log if we read trailing null byte (symaddr + symlen)
	# If keeps incrementing, then log ... hmm... need to think
	def guard_page_exportdir(self,exception):
		try:
			if (self.ntbrkpthit):
				# E.1.1 Get the exception and fault information that we need
				e_addr = exception.get_exception_address()
				f_addr = exception.get_fault_address()
				f_type = exception.get_fault_type()

				if (f_type == winappdbg.win32.EXCEPTION_READ_FAULT):
					p = exception.get_process()
					e_label = p.get_label_at_address(e_addr)
					if (not e_label.startswith("ntdll!")):

						#
						# Non-ntdll code searching through export directory
						#

						# log it

						t = exception.get_thread()
						instr = t.disassemble_instruction(e_addr)[2].lower()
						l = p.get_label_at_address(e_addr)
						log("[*] Memory breakpoint (0x%x) on export directory address 0x%x referenced from 0x%x (%s): %s" % (f_type,f_addr,e_addr,l,instr))
						self.exportdirrdaddrs[e_addr] = instr

						# remove memory breakpoints

						d = exception.debug
						#mem_bps = d.get_all_page_breakpoints()

						for (pid,addr) in self.membps:
							size = self.membps[(pid,addr)]
							d.dont_watch_buffer(pid,addr,size);

						#
						# attempt to find symbol
						# (really slows things down, so removed)
						#
		except:
			traceback.print_exc()
			raise


	### D.4
	# load_dll
	#
	#     winappdbg defined callback function to handle DLL load events
	###

	def load_dll(self,event):
		try:
			log("[*] Load DLL: %s" % event.get_filename())

			baseaddr = event.get_module_base()
			p = event.get_process()
			self.DbgBreakPoint = p.get_system_breakpoint()

			#if (event.get_filename().endswith("ntdll.dll")):
			#	m = event.get_module()

			#	# resolve this here so that it is resolvable in exception()
			#	# below!
			#	self.DbgBreakPoint = m.resolve_symbol("DbgBreakPoint")
			#	log("[D]  DbgBreakPoint = 0x%x   system breakpoint = 0x%x" % (self.DbgBreakPoint,p.get_system_breakpoint()))

			m = event.get_module()
			(addr,size) = self.get_exportdir_names(m)

			d = event.debug
			pid = event.get_pid()
			tid = event.get_tid()

			#
			# Need to uncomment this block if wanting to find symbol
			#
			#pagesize = winappdbg.System.pageSize
			#numpages = int(size / pagesize) + 1
			#pg_start = int(addr / pagesize) * pagesize
			#
			#for pgnum in range(0,numpages):
			#	start = pg_start + (pgnum * pagesize)
			#	#log("[D]     reading page #%d of 0x%x bytes from 0x%x" % (pgnum,pagesize,start))
			#	pg = p.read(start,pagesize)
			#	self.exportdirs[(pid,start,start + pagesize)] = pg

			#d.watch_buffer(pid,addr,size,self.guard_page_exportdir)
			#self.membps[(pid,addr)] = size

			self.eventlog.append({
				"time": time.time(),
				"name": event.get_event_name(),
				"type": "WinAppDbg Event",
				"pid": pid,
				"tid": tid,
				"info": {
					"module_base": event.get_module_base(),
					"filename": event.get_filename(),
				},
			})
		except:
			traceback.print_exc()
			raise


	### D.5
	# event
	#
	#     winappdbg defined callback function to handle any remaining events
	###

	def event(self,event):
		log("[*] Unhandled event: %s" % event.get_event_name())


###
# E. winappdbg debug exception handlers
###

	### E.1
	# guard_page
	#
	#     winappdbg defined callback function to handle guard page exceptions
	###

	def guard_page_exemem(self,exception):
		try:
			f_type = exception.get_fault_type()

			e_addr = exception.get_exception_address()
			f_addr = exception.get_fault_address()

			# get the process and thread ids
			pid = exception.get_pid()
			tid = exception.get_tid()

			# It is interesting to log this, but it generates a lot of log 
			# output and slows the whole process down
			#log("[!] <%d:%d> 0x%x: GUARD_PAGE(%d) exception for address 0x%x" % (pid,tid,e_addr,f_type,f_addr))
			#log("[*] VirtualAlloc()d memory address 0x%x accessed (%d) from 0x%x (%s)" % (f_addr,f_type,e_addr,instr))

			# E.1.2 Was it a memory write operation?
			if (f_type == winappdbg.win32.EXCEPTION_WRITE_FAULT):
				# E.1.2.1 Use the writeaddrs[] array to check to see 
				#         if we have already logged access from this
				#         address, as unpacking is generally done in 
				#         a loop and we don't want to log the same
				#         instructions for each iteration
				if not e_addr in self.writeaddrs:
					p = exception.get_process()
					t = exception.get_thread()
					label = p.get_label_at_address(e_addr)
					instr = t.disassemble_instruction(e_addr)[2].lower()
					log("[*] VirtualAlloc()d memory address 0x%x written from 0x%x (%s): %s" % (f_addr,e_addr,label,instr))
					self.writeaddrs[e_addr] = instr

				# E.1.2.2 Use the tracing variable to see if we have
				#         already started tracing, that is single 
				#         stepping. If not, enable it, and make a note
				#         of the fact by setting the tracing variable
				#         to True
				tid = exception.get_tid()
				if (tid not in self.tracing):
					#d = exception.debug
					#self.tracing[tid] = 0
					#d.start_tracing(tid)
					pass

			# E.1.3 Was it a memory instruction fetch (execute) operation, 
			#       and if so, are we still looking for the entry point address?
			if (f_type == winappdbg.win32.EXCEPTION_EXECUTE_FAULT) and (self.entrypt == 0):
				self.entrypt = e_addr
				t = exception.get_thread()
				jmpinstr = t.disassemble_instruction(self.lasteip[0])[2].lower()

				# E.1.3.1 Log what we've found
				#log("[D]     lasteip[1]: 0x%x" % self.lasteip[1])
				log("[*]     Found unpacked entry point at 0x%x called from 0x%x (%s)" % (self.entrypt,self.lasteip[0],jmpinstr))
				log("[-]     Unpacking loop at 0x%x - 0x%x" % (self.lowesteip,self.highesteip))

				pid = exception.get_pid()
				tid = exception.get_tid()

				elog = ({
					"time": time.time(),
					"name": "unpacking loop found",
					"type": "unpack event",
					"pid": pid,
					"tid": tid,
					"info": {
						"unpacked_entry_point": self.entrypt,
						"callingaddr": self.lasteip[0],
						"callinginstr": jmpinstr
					},
				})

				# E.1.3.2
				for (mem_pid,memblk) in self.allocedmem:
					if (mem_pid == pid):
						size = self.allocedmem[(mem_pid,memblk)]
						endaddr = memblk + size - 1
						if (e_addr >= memblk) and (e_addr <= endaddr):
							# E.1.3.3 Log what we're doing and delete the memory breakpoint
							log("[-]     Dumping %d bytes of memory range 0x%x - 0x%x" % (size,memblk,endaddr))
							d = exception.debug
							d.dont_watch_buffer(exception.get_pid(),memblk,size - 1)

							# E.1.3.4 Disable single-step debugging
							tid = exception.get_tid()
							if not (tid in self.tracing):
								del(self.tracing[tid])
								d.stop_tracing(tid)

							# E.1.3.5 Reset unpacking loop variables
							self.entrypt = 0x00000000
							#del self.lasteip
							self.lasteip = [0x00000000,0x00000000]
							self.lowesteip = 0xffffffff
							self.highest = 0x00000000

							# E.1.3.6 Dump the memory block to a file
							p = exception.get_process()

							filename = sys.argv[1] + ".memblk0x%08x" % memblk
							dumpfile = open(filename,"wb")
							dumpfile.write(p.read(memblk,size))
							dumpfile.close()

							elog["info"]["filename"] = filename
				self.eventlog.append(elog)
		except Exception as e:
			traceback.print_exc()
			raise


	### E.2
	# single_step
	#
	#     winappdbg defined callback function to handle single step exceptions
	###

	def single_step(self,exception):
		try:
			# E.2.1 Get the exception address
			e_addr = exception.get_exception_address()

			# E.2.2 If we have just looped back (eip has gone backward)
			if (e_addr < self.lasteip[1]):
				# Remember this lower address as the lowest loop address
				if self.lowesteip == 0xffffffff: self.lowesteip = e_addr

				# ... and the address we just jumped from as the highest loop address
				if self.highesteip == 0x00000000: self.highesteip = self.lasteip[1]

			# E.2.3 If we are executing an instruction within the bounds of the loop
			#       and we haven't already disassembled this address, then do so
			if (e_addr >= self.lowesteip) and (e_addr <= self.highesteip) and (not e_addr in self.disasmd):
				t = exception.get_thread()
				disasm = t.disassemble_instruction(e_addr)
				instr = disasm[2].lower()
				log("    0x%x: %s" % (e_addr,instr))
				self.disasmd.append(e_addr)

			# E.2.4 Remember the last two instruction addresses (eip values)
			#       We need to remember the last two in order to be able to
			#       disassemble the instruction that jumped to the original 
			#       entry point in the unpacked code
			self.lasteip[0] = self.lasteip[1]
			self.lasteip[1] = e_addr

			tid = exception.get_tid()
			if (self.tracing[tid] == 10000):
				log("[E] Reached tracing limit of 10000 instructions")
				d = exception.debug
				d.stop_tracing(tid)
				self.tracing[tid] = None
		except Exception as e:
			traceback.print_exc()
			raise


	def bp_createprocessresume(self,exception):
		log("[*] Created suspended process resumed")


	### E.3
	# exception
	#
	#     winappdbg defined callback function to handle remaining exceptions
	###

	def exception(self,exception):
		log("[*] Unhandled exception at 0x%x: %s" % (exception.get_exception_address(),exception.get_exception_name()))
		#log("[-]   0x%x fault at 0x%x" % (exception.get_fault_type(),exception.get_fault_address()))

		p = exception.get_process()
		d = exception.debug

		e_addr = exception.get_exception_address()
		self.ntbrkpthit = (e_addr == p.resolve_symbol("DbgBreakPoint"))


#
#### end of MyEventHandler class
#


###
# F. Miscellaneous functions
###

### F.1
# log(msg):
###
def log(msg):
	global logfile

	print(msg)
	if not logfile:
		logfile = open(sys.argv[1] + ".log","w")
	if logfile:
		logfile.write(msg + "\n")
		logfile.flush()

	#logfile.log_text(msg)


### F.2
# simple_debugger(argv):
###
def simple_debugger(filename):
	global logfile

	try:
		handler = MyEventHandler()
		#logfile = winappdbg.textio.Logger(filename + ".log",verbose = True)
	except:
		traceback.print_exc()
	with winappdbg.Debug(handler,bKillOnExit = True,bHostileCode = False) as debug:
		log("[*] Starting %s" % filename)
		debug.execl(filename,bFollow = False)
		log("[*] Starting debug loop")
		debug.loop()
		log("[*] Terminating")

	log("[D] Number of created processes: %d" % len(handler.createdprocesses))
	for i in range(0,len(handler.eventlog)):
		log("%s" % handler.eventlog[i])


###
# G. Start of script execution
###

log("[*] Started at %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
simple_debugger(sys.argv[1])
log("[*] Completed at %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
