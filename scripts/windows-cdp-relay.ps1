param(
    [Parameter(Mandatory = $true)][string]$ListenAddress,
    [Parameter(Mandatory = $true)][int]$ListenPort,
    [Parameter(Mandatory = $true)][int]$TargetPort
)
$ErrorActionPreference = "Stop"

Add-Type -TypeDefinition @"
using System;
using System.Net;
using System.Net.Sockets;
using System.Threading.Tasks;

public static class ResearchSupervisorCdpRelay
{
    public static async Task Run(string address, int listenPort, int targetPort)
    {
        var listener = new TcpListener(IPAddress.Parse(address), listenPort);
        listener.Start();
        while (true)
        {
            var downstream = await listener.AcceptTcpClientAsync();
            Task ignored = Forward(downstream, targetPort);
        }
    }

    private static async Task Forward(TcpClient downstream, int targetPort)
    {
        using (downstream)
        using (var upstream = new TcpClient())
        {
            await upstream.ConnectAsync(IPAddress.Loopback, targetPort);
            using (var downstreamStream = downstream.GetStream())
            using (var upstreamStream = upstream.GetStream())
            {
                var toUpstream = downstreamStream.CopyToAsync(upstreamStream);
                var toDownstream = upstreamStream.CopyToAsync(downstreamStream);
                await Task.WhenAny(toUpstream, toDownstream);
            }
        }
    }
}
"@

[ResearchSupervisorCdpRelay]::Run($ListenAddress, $ListenPort, $TargetPort).GetAwaiter().GetResult()
